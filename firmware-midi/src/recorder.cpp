// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 lightstick-control contributors
#include "recorder.h"

#include <ArduinoJson.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_intr_alloc.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ff.h"
#include "usb/msc_host.h"
#include "usb/msc_host_vfs.h"
#include "usb/usb_host.h"

namespace {

constexpr char MOUNT_PATH[] = "/usb0";
constexpr char RECORDING_DIR[] = "/usb0/lightstick";
constexpr size_t PSRAM_RING_BYTES = 4 * 1024 * 1024;
constexpr size_t EDGE_QUEUE_LENGTH = 2048;
constexpr size_t WRITE_BATCH_RECORDS = 256;
constexpr size_t MAX_CLIENT_CHUNK_RECORDS = 512;
constexpr uint64_t SEGMENT_LIMIT_BYTES = 1024ULL * 1024ULL * 1024ULL;
constexpr uint64_t DRAIN_EVENT_TIMESTAMP = UINT64_MAX;

struct EdgeEvent {
  uint64_t timestampUs;
  uint8_t level;
  uint8_t reserved[7];
};

struct __attribute__((packed)) PulseRecord {
  uint64_t timeUs;
  uint8_t level;
  uint8_t flags;
  uint16_t reserved16;
  uint32_t reserved32;
};

struct __attribute__((packed)) RecordingHeader {
  char magic[4];
  uint16_t version;
  uint16_t headerSize;
  uint16_t recordSize;
  uint16_t reserved16;
  uint32_t frequencyHz;
  uint32_t bandwidthHz;
  uint32_t dataRateMilliKbaud;
  uint8_t modulation;
  uint8_t startLevel;
  uint8_t reserved[6];
  uint64_t utcEpochMs;
};

static_assert(sizeof(PulseRecord) == 16, "Pulse record must remain stable");
static_assert(sizeof(RecordingHeader) == 40, "LSR1 header must remain stable");

enum class RecordingTarget : uint8_t {
  None,
  Usb,
  Client,
};

struct UsbEventMessage {
  msc_host_event_t event;
};

uint8_t capturePin = 0;
QueueHandle_t edgeQueue = nullptr;
QueueHandle_t usbEventQueue = nullptr;
SemaphoreHandle_t fileMutex = nullptr;
SemaphoreHandle_t stateMutex = nullptr;
SemaphoreHandle_t captureDrainSemaphore = nullptr;
portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

PulseRecord* psramRing = nullptr;
size_t ringCapacity = 0;
volatile size_t ringHead = 0;
volatile size_t ringTail = 0;

volatile bool captureEnabled = false;
volatile bool recordingActive = false;
volatile uint32_t droppedIsrEdges = 0;
volatile RecordingTarget recordingTarget = RecordingTarget::None;
uint64_t captureStartUs = 0;
uint64_t recordingStartedMs = 0;
uint64_t recordingStoppedElapsedMs = 0;
uint64_t recordingUtcEpochMs = 0;
uint64_t capturedEdges = 0;
uint64_t writtenEdges = 0;
uint64_t droppedPsramEdges = 0;
uint64_t filteredEdges = 0;

bool psramReady = false;
bool usbHostReady = false;
volatile bool usbMounted = false;
String lastError;
String recordingName;
String currentFilePath;
ReceiverConfig activeConfig;
RecordingConfig activeRecordingConfig;
uint8_t recordingStartLevel = 0;
String recordingStopReason;
uint32_t currentSegmentIndex = 0;
uint64_t currentSegmentBytes = 0;
FILE* currentFile = nullptr;

bool clientRecordingAvailable = false;
String clientSessionId;
uint64_t clientAcknowledgedRecords = 0;
uint64_t clientPendingStart = 0;
uint64_t clientPendingEnd = 0;
size_t clientPendingRecords = 0;

enum class AutomaticStopReason : uint8_t {
  None,
  MaxDuration,
  MaxEdges,
  BufferHighWater,
  BufferOverflow,
  StorageFailure,
};

volatile AutomaticStopReason automaticStopReason = AutomaticStopReason::None;
volatile bool stopOnBufferOverflowActive = true;
volatile uint64_t lastAcceptedEdgeTimestampUs = 0;

msc_host_device_handle_t usbDevice = nullptr;
msc_host_vfs_handle_t usbVfs = nullptr;

String espError(const char* operation, esp_err_t error) {
  String message(operation);
  message += ": ";
  message += esp_err_to_name(error);
  return message;
}

const char* recordingTargetName(RecordingTarget target) {
  switch (target) {
    case RecordingTarget::Usb:
      return "usb";
    case RecordingTarget::Client:
      return "client";
    case RecordingTarget::None:
    default:
      return "none";
  }
}

const char* edgeModeName(RecordingEdgeMode mode) {
  switch (mode) {
    case RecordingEdgeMode::Rising:
      return "rising";
    case RecordingEdgeMode::Falling:
      return "falling";
    case RecordingEdgeMode::Both:
    default:
      return "both";
  }
}

int interruptMode(RecordingEdgeMode mode) {
  switch (mode) {
    case RecordingEdgeMode::Rising:
      return RISING;
    case RecordingEdgeMode::Falling:
      return FALLING;
    case RecordingEdgeMode::Both:
    default:
      return CHANGE;
  }
}

const char* automaticStopReasonName(AutomaticStopReason reason) {
  switch (reason) {
    case AutomaticStopReason::MaxDuration:
      return "max_duration";
    case AutomaticStopReason::MaxEdges:
      return "max_edges";
    case AutomaticStopReason::BufferHighWater:
      return "buffer_high_water";
    case AutomaticStopReason::BufferOverflow:
      return "buffer_overflow";
    case AutomaticStopReason::StorageFailure:
      return "storage_failure";
    case AutomaticStopReason::None:
    default:
      return "manual";
  }
}

String encodeBase64(const uint8_t* bytes, size_t length) {
  static constexpr char alphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  String encoded;
  encoded.reserve(((length + 2) / 3) * 4);
  for (size_t offset = 0; offset < length; offset += 3) {
    const uint32_t first = bytes[offset];
    const uint32_t second = offset + 1 < length ? bytes[offset + 1] : 0;
    const uint32_t third = offset + 2 < length ? bytes[offset + 2] : 0;
    const uint32_t value = (first << 16) | (second << 8) | third;
    encoded += alphabet[(value >> 18) & 0x3F];
    encoded += alphabet[(value >> 12) & 0x3F];
    encoded += offset + 1 < length ? alphabet[(value >> 6) & 0x3F] : '=';
    encoded += offset + 2 < length ? alphabet[value & 0x3F] : '=';
  }
  return encoded;
}

uint32_t crc32(const uint8_t* bytes, size_t length) {
  uint32_t crc = 0xFFFFFFFFUL;
  for (size_t index = 0; index < length; ++index) {
    crc ^= bytes[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xEDB88320UL & (0UL - (crc & 1UL)));
    }
  }
  return ~crc;
}

RecordingHeader makeRecordingHeader() {
  RecordingHeader header{};
  memcpy(header.magic, "LSR1", 4);
  header.version = 1;
  header.headerSize = sizeof(header);
  header.recordSize = sizeof(PulseRecord);
  header.frequencyHz = activeConfig.frequencyHz;
  header.bandwidthHz = activeConfig.bandwidthHz;
  header.dataRateMilliKbaud = static_cast<uint32_t>(activeConfig.dataRateKbaud * 1000.0f);
  header.modulation = activeConfig.modulation;
  header.startLevel = recordingStartLevel;
  header.utcEpochMs = recordingUtcEpochMs;
  return header;
}

String sanitizeName(const String& requested) {
  String value;
  value.reserve(48);
  for (size_t index = 0; index < requested.length() && value.length() < 40; ++index) {
    const char character = requested[index];
    if (isalnum(static_cast<unsigned char>(character)) || character == '-' || character == '_') {
      value += character;
    } else if (character == ' ' && !value.endsWith("_")) {
      value += '_';
    }
  }
  if (value.isEmpty()) {
    value = "capture_";
    value += String(millis());
  }
  return value;
}

size_t ringUsedRecords() {
  portENTER_CRITICAL(&ringMux);
  const size_t head = ringHead;
  const size_t tail = ringTail;
  const size_t used = head >= tail ? head - tail : ringCapacity - tail + head;
  portEXIT_CRITICAL(&ringMux);
  return used;
}

bool clientRecordingHasUnreadData() {
  return clientRecordingAvailable && recordingTarget == RecordingTarget::Client
    && (clientPendingRecords > 0 || ringUsedRecords() > 0);
}

bool pushRing(const PulseRecord& record) {
  bool stored = false;
  portENTER_CRITICAL(&ringMux);
  const size_t next = (ringHead + 1) % ringCapacity;
  if (next != ringTail) {
    psramRing[ringHead] = record;
    ringHead = next;
    stored = true;
  } else {
    ++droppedPsramEdges;
    if (stopOnBufferOverflowActive) automaticStopReason = AutomaticStopReason::BufferOverflow;
  }
  portEXIT_CRITICAL(&ringMux);
  return stored;
}

size_t popRing(PulseRecord* output, size_t maximum) {
  size_t count = 0;
  while (count < maximum) {
    portENTER_CRITICAL(&ringMux);
    if (ringTail == ringHead) {
      portEXIT_CRITICAL(&ringMux);
      break;
    }
    output[count] = psramRing[ringTail];
    ringTail = (ringTail + 1) % ringCapacity;
    portEXIT_CRITICAL(&ringMux);
    ++count;
  }
  return count;
}

bool restoreRingFront(const PulseRecord* records, size_t count) {
  if (count == 0) return true;
  portENTER_CRITICAL(&ringMux);
  const size_t used = ringHead >= ringTail ? ringHead - ringTail : ringCapacity - ringTail + ringHead;
  const size_t freeSlots = ringCapacity > used ? ringCapacity - 1 - used : 0;
  if (count > freeSlots) {
    portEXIT_CRITICAL(&ringMux);
    return false;
  }
  const size_t newTail = (ringTail + ringCapacity - (count % ringCapacity)) % ringCapacity;
  for (size_t index = 0; index < count; ++index) {
    psramRing[(newTail + index) % ringCapacity] = records[index];
  }
  ringTail = newTail;
  portEXIT_CRITICAL(&ringMux);
  return true;
}

size_t discardRingRecords(size_t maximum) {
  portENTER_CRITICAL(&ringMux);
  const size_t used = ringHead >= ringTail ? ringHead - ringTail : ringCapacity - ringTail + ringHead;
  const size_t discarded = min(used, maximum);
  ringTail = (ringTail + discarded) % ringCapacity;
  portEXIT_CRITICAL(&ringMux);
  return discarded;
}

size_t copyRingRecords(std::vector<PulseRecord>& output, size_t maximum) {
  portENTER_CRITICAL(&ringMux);
  const size_t used = ringHead >= ringTail ? ringHead - ringTail : ringCapacity - ringTail + ringHead;
  const size_t count = min(used, maximum);
  portEXIT_CRITICAL(&ringMux);

  output.resize(count);

  portENTER_CRITICAL(&ringMux);
  const size_t currentUsed = ringHead >= ringTail ? ringHead - ringTail : ringCapacity - ringTail + ringHead;
  const size_t copied = min(currentUsed, count);
  for (size_t index = 0; index < copied; ++index) {
    output[index] = psramRing[(ringTail + index) % ringCapacity];
  }
  portEXIT_CRITICAL(&ringMux);
  output.resize(copied);
  return copied;
}

void IRAM_ATTR edgeInterrupt() {
  if (!captureEnabled || edgeQueue == nullptr) return;
  EdgeEvent event{};
  event.timestampUs = static_cast<uint64_t>(esp_timer_get_time());
  event.level = static_cast<uint8_t>(gpio_get_level(static_cast<gpio_num_t>(capturePin)));
  BaseType_t wake = pdFALSE;
  if (xQueueSendFromISR(edgeQueue, &event, &wake) != pdTRUE) {
    ++droppedIsrEdges;
    if (stopOnBufferOverflowActive) automaticStopReason = AutomaticStopReason::BufferOverflow;
  }
  if (wake == pdTRUE) portYIELD_FROM_ISR();
}

bool writeMetadataLocked(bool completed) {
  if (!usbMounted || recordingName.isEmpty()) return false;
  const String path = String(RECORDING_DIR) + "/" + recordingName + ".json";
  FILE* metadata = fopen(path.c_str(), "w");
  if (metadata == nullptr) {
    lastError = String("metadata open failed: ") + strerror(errno);
    return false;
  }

  JsonDocument document;
  document["format"] = "LSR1";
  document["format_version"] = 1;
  document["record_size_bytes"] = sizeof(PulseRecord);
  document["record_layout"] = "uint64 time_us, uint8 level, uint8 flags, 6 reserved bytes";
  document["name"] = recordingName;
  document["state"] = completed ? "complete" : "recording";
  document["frequency_hz"] = activeConfig.frequencyHz;
  document["rx_bandwidth_hz"] = activeConfig.bandwidthHz;
  document["modulation"] = activeConfig.modulation;
  document["data_rate_kbaud"] = activeConfig.dataRateKbaud;
  document["edge_mode"] = edgeModeName(activeRecordingConfig.edgeMode);
  document["min_edge_interval_us"] = activeRecordingConfig.minEdgeIntervalUs;
  document["max_duration_ms"] = activeRecordingConfig.maxDurationMs;
  document["max_edges"] = activeRecordingConfig.maxEdges;
  document["client_chunk_records"] = activeRecordingConfig.clientChunkRecords;
  document["buffer_stop_threshold_percent"] = activeRecordingConfig.bufferStopThresholdPercent;
  document["stop_on_buffer_overflow"] = activeRecordingConfig.stopOnBufferOverflow;
  document["start_level"] = recordingStartLevel;
  document["utc_epoch_ms"] = recordingUtcEpochMs;
  document["duration_ms"] = completed ? recordingStoppedElapsedMs : millis() - recordingStartedMs;
  document["captured_edges"] = capturedEdges;
  document["written_edges"] = writtenEdges;
  document["dropped_isr_edges"] = droppedIsrEdges;
  document["dropped_psram_edges"] = droppedPsramEdges;
  document["filtered_edges"] = filteredEdges;
  document["stop_reason"] = recordingStopReason;
  document["segment_count"] = currentSegmentIndex + 1;
  String json;
  serializeJsonPretty(document, json);
  fwrite(json.c_str(), 1, json.length(), metadata);
  fputc('\n', metadata);
  fclose(metadata);
  return true;
}

bool recordingNameCollisionLocked(const String& name, String& existingPath) {
  const String prefix = String(RECORDING_DIR) + "/" + name;
  const char* suffixes[] = {
    ".json",
    "_000.lsr",
    "_000.lsr.part",
    ".lsr",
    ".lsr.part",
  };
  struct stat existing{};
  for (const char* suffix : suffixes) {
    const String path = prefix + suffix;
    if (stat(path.c_str(), &existing) == 0) {
      existingPath = path;
      return true;
    }
  }
  return false;
}

bool openSegmentLocked(String& error) {
  char suffix[16];
  snprintf(suffix, sizeof(suffix), "_%03lu.lsr", static_cast<unsigned long>(currentSegmentIndex));
  currentFilePath = String(RECORDING_DIR) + "/" + recordingName + suffix;
  const int descriptor = open(currentFilePath.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0664);
  if (descriptor < 0) {
    error = String("recording open failed: ") + strerror(errno);
    lastError = error;
    currentFilePath = "";
    return false;
  }
  currentFile = fdopen(descriptor, "wb");
  if (currentFile == nullptr) {
    const int savedErrno = errno;
    close(descriptor);
    unlink(currentFilePath.c_str());
    error = String("recording stream setup failed: ") + strerror(savedErrno);
    lastError = error;
    currentFilePath = "";
    return false;
  }
  setvbuf(currentFile, nullptr, _IOFBF, 16 * 1024);

  const RecordingHeader header = makeRecordingHeader();
  if (fwrite(&header, sizeof(header), 1, currentFile) != 1) {
    error = String("recording header write failed: ") + strerror(errno);
    lastError = error;
    fclose(currentFile);
    currentFile = nullptr;
    unlink(currentFilePath.c_str());
    currentFilePath = "";
    currentSegmentBytes = 0;
    return false;
  }
  currentSegmentBytes = sizeof(header);
  return true;
}

void captureTask(void*) {
  EdgeEvent event{};
  while (true) {
    if (xQueueReceive(edgeQueue, &event, portMAX_DELAY) != pdTRUE) continue;
    if (event.timestampUs == DRAIN_EVENT_TIMESTAMP) {
      xSemaphoreGive(captureDrainSemaphore);
      continue;
    }
    if (!recordingActive || event.timestampUs < captureStartUs) continue;
    if (!captureEnabled && automaticStopReason != AutomaticStopReason::None) continue;
    if (activeRecordingConfig.minEdgeIntervalUs > 0 && lastAcceptedEdgeTimestampUs > 0
        && event.timestampUs - lastAcceptedEdgeTimestampUs < activeRecordingConfig.minEdgeIntervalUs) {
      ++filteredEdges;
      continue;
    }
    lastAcceptedEdgeTimestampUs = event.timestampUs;
    PulseRecord record{};
    record.timeUs = event.timestampUs - captureStartUs;
    record.level = event.level;
    if (pushRing(record)) {
      ++capturedEdges;
      if (ringUsedRecords() * 100ULL
          >= ringCapacity * activeRecordingConfig.bufferStopThresholdPercent) {
        captureEnabled = false;
        automaticStopReason = AutomaticStopReason::BufferHighWater;
      }
      if (activeRecordingConfig.maxEdges > 0 && capturedEdges >= activeRecordingConfig.maxEdges) {
        captureEnabled = false;
        automaticStopReason = AutomaticStopReason::MaxEdges;
      }
    }
  }
}

void writerTask(void*) {
  PulseRecord batch[WRITE_BATCH_RECORDS];
  uint32_t lastFlushMs = millis();
  while (true) {
    if (recordingTarget == RecordingTarget::Client) {
      // Client reads acknowledge records from this ring; the USB writer must not consume them.
      delay(2);
      continue;
    }
    if (ringUsedRecords() == 0) {
      delay(2);
      continue;
    }

    if (currentFile == nullptr) {
      // A storage failure stops capture and leaves the remaining ring tail for
      // stop() to account; never pop it into nowhere.
      delay(2);
      continue;
    }

    if (xSemaphoreTake(fileMutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
      continue;
    }
    const size_t count = popRing(batch, WRITE_BATCH_RECORDS);
    if (count == 0) {
      xSemaphoreGive(fileMutex);
      continue;
    }

    if (currentFile != nullptr) {
      const size_t bytes = count * sizeof(PulseRecord);
      if (currentSegmentBytes + bytes > SEGMENT_LIMIT_BYTES) {
        fflush(currentFile);
        fclose(currentFile);
        currentFile = nullptr;
        ++currentSegmentIndex;
        String openError;
        if (!openSegmentLocked(openError)) {
          captureEnabled = false;
          automaticStopReason = AutomaticStopReason::StorageFailure;
          detachInterrupt(digitalPinToInterrupt(capturePin));
          lastError = openError.isEmpty() ? "USB segment rollover failed" : openError;
          recordingStopReason = "usb_rollover_failed";
          recordingStoppedElapsedMs = millis() - recordingStartedMs;
          recordingActive = false;
          if (currentSegmentIndex > 0) --currentSegmentIndex;
          currentSegmentBytes = 0;
          if (!restoreRingFront(batch, count)) {
            portENTER_CRITICAL(&ringMux);
            droppedPsramEdges += count;
            portEXIT_CRITICAL(&ringMux);
          }
        }
      }
      if (currentFile != nullptr) {
        const size_t written = fwrite(batch, sizeof(PulseRecord), count, currentFile);
        writtenEdges += written;
        currentSegmentBytes += written * sizeof(PulseRecord);
        if (written != count) {
          lastError = String("USB write failed: ") + strerror(errno);
          portENTER_CRITICAL(&ringMux);
          droppedPsramEdges += count - written;
          portEXIT_CRITICAL(&ringMux);
          recordingStopReason = "usb_write_failed";
          recordingStoppedElapsedMs = millis() - recordingStartedMs;
          recordingActive = false;
          captureEnabled = false;
          automaticStopReason = AutomaticStopReason::StorageFailure;
          detachInterrupt(digitalPinToInterrupt(capturePin));
        }
        if (millis() - lastFlushMs >= 1000) {
          fflush(currentFile);
          lastFlushMs = millis();
        }
      }
    } else {
      portENTER_CRITICAL(&ringMux);
      droppedPsramEdges += count;
      portEXIT_CRITICAL(&ringMux);
    }
    xSemaphoreGive(fileMutex);
  }
}

void usbDaemonTask(void*) {
  while (true) {
    uint32_t eventFlags = 0;
    usb_host_lib_handle_events(portMAX_DELAY, &eventFlags);
  }
}

void mscEventCallback(const msc_host_event_t* event, void*) {
  if (event == nullptr || usbEventQueue == nullptr) return;
  UsbEventMessage message{};
  message.event = *event;
  xQueueSend(usbEventQueue, &message, 0);
}

void storageTask(void*) {
  UsbEventMessage message{};
  while (true) {
    if (xQueueReceive(usbEventQueue, &message, portMAX_DELAY) != pdTRUE) continue;
    if (message.event.event == msc_host_event_t::MSC_DEVICE_CONNECTED) {
      if (usbDevice != nullptr) continue;
      esp_err_t result = msc_host_install_device(message.event.device.address, &usbDevice);
      if (result != ESP_OK) {
        lastError = espError("USB device install failed", result);
        usbDevice = nullptr;
        continue;
      }
      const esp_vfs_fat_mount_config_t mountConfig = {
        .format_if_mount_failed = false,
        .max_files = 8,
        .allocation_unit_size = 0,
      };
      result = msc_host_vfs_register(usbDevice, MOUNT_PATH, &mountConfig, &usbVfs);
      if (result != ESP_OK) {
        lastError = espError("FAT32 mount failed", result);
        msc_host_uninstall_device(usbDevice);
        usbDevice = nullptr;
        usbVfs = nullptr;
        continue;
      }
      mkdir(RECORDING_DIR, 0775);
      usbMounted = true;
      lastError = "";
    } else if (message.event.event == msc_host_event_t::MSC_DEVICE_DISCONNECTED) {
      usbMounted = false;
      if (recordingTarget == RecordingTarget::Usb
          && (recordingActive || currentFile != nullptr || automaticStopReason != AutomaticStopReason::None)) {
        String stopError;
        recorder.stop(stopError);
        if (!stopError.isEmpty()) lastError = stopError;
      }
      if (usbVfs != nullptr) {
        msc_host_vfs_unregister(usbVfs);
        usbVfs = nullptr;
      }
      if (usbDevice != nullptr) {
        msc_host_uninstall_device(usbDevice);
        usbDevice = nullptr;
      }
    }
  }
}

}  // namespace

LightstickRecorder recorder;

bool LightstickRecorder::begin(uint8_t inputPin, String& error) {
  capturePin = inputPin;
  fileMutex = xSemaphoreCreateMutex();
  stateMutex = xSemaphoreCreateMutex();
  captureDrainSemaphore = xSemaphoreCreateBinary();
  edgeQueue = xQueueCreate(EDGE_QUEUE_LENGTH, sizeof(EdgeEvent));
  usbEventQueue = xQueueCreate(8, sizeof(UsbEventMessage));
  if (fileMutex == nullptr || stateMutex == nullptr || captureDrainSemaphore == nullptr
      || edgeQueue == nullptr || usbEventQueue == nullptr) {
    error = "failed to allocate recorder RTOS primitives";
    lastError = error;
    return false;
  }

  if (!psramFound() || ESP.getPsramSize() < 7 * 1024 * 1024) {
    error = "N16R8 Octal PSRAM not detected or smaller than 8 MB";
    lastError = error;
    return false;
  }
  psramRing = static_cast<PulseRecord*>(
    heap_caps_malloc(PSRAM_RING_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
  );
  if (psramRing == nullptr) {
    error = "failed to allocate 4 MiB PSRAM recording ring";
    lastError = error;
    return false;
  }
  ringCapacity = PSRAM_RING_BYTES / sizeof(PulseRecord);
  psramReady = true;

  xTaskCreatePinnedToCore(captureTask, "edge-capture", 4096, nullptr, 6, nullptr, 0);
  xTaskCreatePinnedToCore(writerTask, "usb-writer", 6144, nullptr, 3, nullptr, 1);

  const usb_host_config_t hostConfig = {
    .skip_phy_setup = false,
    .intr_flags = ESP_INTR_FLAG_LEVEL1,
  };
  esp_err_t result = usb_host_install(&hostConfig);
  if (result != ESP_OK) {
    error = espError("USB Host install failed", result);
    lastError = error;
    return false;
  }
  xTaskCreatePinnedToCore(usbDaemonTask, "usb-daemon", 4096, nullptr, 2, nullptr, 0);

  const msc_host_driver_config_t mscConfig = {
    .create_backround_task = true,
    .task_priority = 3,
    .stack_size = 4096,
    .core_id = 0,
    .callback = mscEventCallback,
    .callback_arg = nullptr,
  };
  result = msc_host_install(&mscConfig);
  if (result != ESP_OK) {
    error = espError("USB MSC install failed", result);
    lastError = error;
    return false;
  }
  xTaskCreatePinnedToCore(storageTask, "usb-storage", 6144, nullptr, 3, nullptr, 0);
  usbHostReady = true;
  return true;
}

bool LightstickRecorder::start(
  const String& requestedName,
  const ReceiverConfig& config,
  const RecordingConfig& recordingConfig,
  uint64_t utcEpochMs,
  String& error
) {
  if (!psramReady) {
    error = "PSRAM recorder is not ready";
    return false;
  }
  if (stateMutex == nullptr || xSemaphoreTake(stateMutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    error = "recorder state is busy";
    return false;
  }
  if (!usbMounted) {
    error = "FAT32 USB drive is not mounted";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (recordingActive) {
    error = "recording is already active";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (clientRecordingHasUnreadData()) {
    error = "client recording has unread data; continue READ_CLIENT_RECORDING until end_of_stream";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (currentFile != nullptr) {
    error = "recorder has an open file; stop recording before restarting";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (xSemaphoreTake(fileMutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
    error = "recorder is busy";
    xSemaphoreGive(stateMutex);
    return false;
  }

  const String requestedRecordingName = sanitizeName(requestedName);
  String existingPath;
  if (recordingNameCollisionLocked(requestedRecordingName, existingPath)) {
    error = String("recording name already exists: ") + existingPath;
    xSemaphoreGive(fileMutex);
    xSemaphoreGive(stateMutex);
    return false;
  }

  captureEnabled = false;
  detachInterrupt(digitalPinToInterrupt(capturePin));
  xQueueReset(edgeQueue);
  xSemaphoreTake(captureDrainSemaphore, 0);
  portENTER_CRITICAL(&ringMux);
  ringHead = 0;
  ringTail = 0;
  droppedPsramEdges = 0;
  portEXIT_CRITICAL(&ringMux);
  droppedIsrEdges = 0;
  filteredEdges = 0;
  capturedEdges = 0;
  writtenEdges = 0;
  recordingStoppedElapsedMs = 0;
  activeConfig = config;
  activeRecordingConfig = recordingConfig;
  stopOnBufferOverflowActive = recordingConfig.stopOnBufferOverflow;
  automaticStopReason = AutomaticStopReason::None;
  lastAcceptedEdgeTimestampUs = 0;
  recordingUtcEpochMs = utcEpochMs;
  recordingName = requestedRecordingName;
  currentSegmentIndex = 0;
  currentSegmentBytes = 0;
  currentFilePath = "";
  recordingTarget = RecordingTarget::Usb;
  clientRecordingAvailable = false;
  clientSessionId = "";
  clientAcknowledgedRecords = 0;
  clientPendingStart = 0;
  clientPendingEnd = 0;
  clientPendingRecords = 0;
  lastError = "";
  recordingStopReason = "active";
  recordingStartLevel = digitalRead(capturePin) ? 1 : 0;

  if (!openSegmentLocked(error)) {
    recordingTarget = RecordingTarget::None;
    xSemaphoreGive(fileMutex);
    xSemaphoreGive(stateMutex);
    return false;
  }
  recordingStartedMs = millis();
  captureStartUs = static_cast<uint64_t>(esp_timer_get_time());
  recordingActive = true;
  writeMetadataLocked(false);
  xSemaphoreGive(fileMutex);

  pinMode(capturePin, INPUT);
  attachInterrupt(
    digitalPinToInterrupt(capturePin),
    edgeInterrupt,
    interruptMode(activeRecordingConfig.edgeMode)
  );
  captureEnabled = true;
  xSemaphoreGive(stateMutex);
  return true;
}

bool LightstickRecorder::startClient(
  const String& requestedName,
  const ReceiverConfig& config,
  const RecordingConfig& recordingConfig,
  uint64_t utcEpochMs,
  ClientRecordingStart& result,
  String& error
) {
  if (!psramReady) {
    error = "PSRAM recorder is not ready";
    return false;
  }
  if (stateMutex == nullptr || xSemaphoreTake(stateMutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    error = "recorder state is busy";
    return false;
  }
  if (recordingActive) {
    error = "recording is already active";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (clientRecordingHasUnreadData()) {
    error = "client recording has unread data; continue READ_CLIENT_RECORDING until end_of_stream";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (currentFile != nullptr) {
    error = "recorder has an open file; stop recording before restarting";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (xSemaphoreTake(fileMutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
    error = "recorder is busy";
    xSemaphoreGive(stateMutex);
    return false;
  }

  captureEnabled = false;
  detachInterrupt(digitalPinToInterrupt(capturePin));
  xQueueReset(edgeQueue);
  xSemaphoreTake(captureDrainSemaphore, 0);
  portENTER_CRITICAL(&ringMux);
  ringHead = 0;
  ringTail = 0;
  droppedPsramEdges = 0;
  portEXIT_CRITICAL(&ringMux);
  droppedIsrEdges = 0;
  filteredEdges = 0;
  capturedEdges = 0;
  writtenEdges = 0;
  recordingStoppedElapsedMs = 0;
  activeConfig = config;
  activeRecordingConfig = recordingConfig;
  stopOnBufferOverflowActive = recordingConfig.stopOnBufferOverflow;
  automaticStopReason = AutomaticStopReason::None;
  lastAcceptedEdgeTimestampUs = 0;
  recordingUtcEpochMs = utcEpochMs;
  recordingName = sanitizeName(requestedName);
  currentSegmentIndex = 0;
  currentSegmentBytes = 0;
  currentFilePath = "";
  recordingTarget = RecordingTarget::Client;
  clientRecordingAvailable = true;
  clientSessionId = String("client-") + String(millis()) + '-' + String(esp_random(), HEX);
  clientAcknowledgedRecords = 0;
  clientPendingStart = 0;
  clientPendingEnd = 0;
  clientPendingRecords = 0;
  lastError = "";
  recordingStopReason = "active";
  recordingStartLevel = digitalRead(capturePin) ? 1 : 0;
  recordingStartedMs = millis();
  captureStartUs = static_cast<uint64_t>(esp_timer_get_time());
  recordingActive = true;

  const RecordingHeader header = makeRecordingHeader();
  result.sessionId = clientSessionId;
  result.recordingName = recordingName;
  result.headerBase64 = encodeBase64(reinterpret_cast<const uint8_t*>(&header), sizeof(header));
  xSemaphoreGive(fileMutex);

  pinMode(capturePin, INPUT);
  attachInterrupt(
    digitalPinToInterrupt(capturePin),
    edgeInterrupt,
    interruptMode(activeRecordingConfig.edgeMode)
  );
  captureEnabled = true;
  xSemaphoreGive(stateMutex);
  return true;
}

bool LightstickRecorder::stop(String& error, const String& reason) {
  if (stateMutex == nullptr || xSemaphoreTake(stateMutex, pdMS_TO_TICKS(6000)) != pdTRUE) {
    error = "recorder stop is already in progress";
    return false;
  }
  const RecordingTarget target = recordingTarget;
  captureEnabled = false;
  detachInterrupt(digitalPinToInterrupt(capturePin));
  if (!recordingActive && currentFile == nullptr
      && !(target == RecordingTarget::Usb && automaticStopReason != AutomaticStopReason::None)) {
    recordingTarget = RecordingTarget::None;
    automaticStopReason = AutomaticStopReason::None;
    xSemaphoreGive(stateMutex);
    return true;
  }

  EdgeEvent drainEvent{};
  drainEvent.timestampUs = DRAIN_EVENT_TIMESTAMP;
  if (xQueueSend(edgeQueue, &drainEvent, pdMS_TO_TICKS(500)) != pdTRUE
      || xSemaphoreTake(captureDrainSemaphore, pdMS_TO_TICKS(500)) != pdTRUE) {
    error = "timed out while draining capture queue";
    lastError = error;
  }
  recordingActive = false;
  recordingStoppedElapsedMs = millis() - recordingStartedMs;
  recordingStopReason = reason;
  automaticStopReason = AutomaticStopReason::None;

  if (target == RecordingTarget::Client) {
    // Leave the PSRAM tail intact until the client acknowledges every chunk.
    xSemaphoreGive(stateMutex);
    return error.isEmpty();
  }

  const uint32_t writeDeadline = millis() + 3000;
  while (ringUsedRecords() > 0 && static_cast<int32_t>(writeDeadline - millis()) > 0) delay(5);
  if (ringUsedRecords() > 0) {
    error = "timed out while draining PSRAM recording buffer";
    lastError = error;
    const size_t stranded = ringUsedRecords();
    portENTER_CRITICAL(&ringMux);
    droppedPsramEdges += stranded;
    portEXIT_CRITICAL(&ringMux);
    discardRingRecords(stranded);
  }

  if (xSemaphoreTake(fileMutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    error = "timed out while closing recording";
    lastError = error;
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (currentFile != nullptr) {
    fflush(currentFile);
    fclose(currentFile);
    currentFile = nullptr;
  }
  if (target == RecordingTarget::Usb && usbMounted) writeMetadataLocked(true);
  recordingTarget = RecordingTarget::None;
  xSemaphoreGive(fileMutex);
  xSemaphoreGive(stateMutex);
  return error.isEmpty();
}

bool LightstickRecorder::stopClient(const String& sessionId, String& error) {
  if (stateMutex == nullptr || xSemaphoreTake(stateMutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    error = "recorder state is busy";
    return false;
  }
  const bool available = clientRecordingAvailable && recordingTarget == RecordingTarget::Client;
  const bool sessionMatches = sessionId == clientSessionId;
  const bool active = recordingActive;
  xSemaphoreGive(stateMutex);

  if (!available) {
    error = "no client recording is available";
    return false;
  }
  if (!sessionMatches) {
    error = "client recording session_id does not match";
    return false;
  }
  return active ? stop(error, "manual") : true;
}

bool LightstickRecorder::serviceAutomaticStop(String& error) {
  AutomaticStopReason reason = automaticStopReason;
  if (recordingTarget == RecordingTarget::None) return false;
  if (!recordingActive && reason == AutomaticStopReason::None) return false;
  if (reason == AutomaticStopReason::None && activeRecordingConfig.maxDurationMs > 0
      && millis() - recordingStartedMs >= activeRecordingConfig.maxDurationMs) {
    reason = AutomaticStopReason::MaxDuration;
  }
  if (reason == AutomaticStopReason::None) return false;

  const String reasonText = automaticStopReasonName(reason);
  stop(error, reasonText);
  return true;
}

bool LightstickRecorder::readClient(
  const String& sessionId,
  uint64_t acknowledgement,
  size_t maximumRecords,
  ClientRecordingChunk& result,
  String& error
) {
  if (maximumRecords == 0 || maximumRecords > MAX_CLIENT_CHUNK_RECORDS) {
    error = "max_records must be in 1..512";
    return false;
  }
  if (stateMutex == nullptr || xSemaphoreTake(stateMutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    error = "recorder state is busy";
    return false;
  }
  if (!clientRecordingAvailable || recordingTarget != RecordingTarget::Client) {
    error = "no client recording is available";
    xSemaphoreGive(stateMutex);
    return false;
  }
  if (sessionId != clientSessionId) {
    error = "client recording session_id does not match";
    xSemaphoreGive(stateMutex);
    return false;
  }

  if (clientPendingRecords > 0) {
    if (acknowledgement == clientPendingEnd) {
      if (discardRingRecords(clientPendingRecords) != clientPendingRecords) {
        error = "client recording buffer changed before acknowledgement";
        xSemaphoreGive(stateMutex);
        return false;
      }
      writtenEdges += clientPendingRecords;
      clientAcknowledgedRecords = clientPendingEnd;
      clientPendingStart = clientPendingEnd;
      clientPendingRecords = 0;
    } else if (acknowledgement != clientAcknowledgedRecords) {
      error = "ack_sequence must equal the current or pending sequence_end";
      xSemaphoreGive(stateMutex);
      return false;
    }
  } else if (acknowledgement != clientAcknowledgedRecords) {
    error = "ack_sequence does not match the client recording cursor";
    xSemaphoreGive(stateMutex);
    return false;
  }

  if (clientPendingRecords == 0) {
    const size_t availableRecords = ringUsedRecords();
    clientPendingRecords = min(availableRecords, maximumRecords);
    clientPendingStart = clientAcknowledgedRecords;
    clientPendingEnd = clientPendingStart + clientPendingRecords;
  }

  std::vector<PulseRecord> records;
  if (clientPendingRecords > 0
      && copyRingRecords(records, clientPendingRecords) != clientPendingRecords) {
    error = "client recording buffer changed while preparing a chunk";
    xSemaphoreGive(stateMutex);
    return false;
  }

  result.sessionId = clientSessionId;
  result.recordingName = recordingName;
  result.sequenceStart = clientPendingStart;
  result.sequenceEnd = clientPendingEnd;
  result.offsetBytes = sizeof(RecordingHeader) + result.sequenceStart * sizeof(PulseRecord);
  result.nextOffsetBytes = sizeof(RecordingHeader) + result.sequenceEnd * sizeof(PulseRecord);
  result.recordCount = clientPendingRecords;
  result.active = recordingActive;
  result.endOfStream = !recordingActive && clientPendingRecords == 0 && ringUsedRecords() == 0;
  xSemaphoreGive(stateMutex);

  if (!records.empty()) {
    result.dataCrc32 = crc32(
      reinterpret_cast<const uint8_t*>(records.data()),
      records.size() * sizeof(PulseRecord)
    );
    result.dataBase64 = encodeBase64(
      reinterpret_cast<const uint8_t*>(records.data()),
      records.size() * sizeof(PulseRecord)
    );
  } else {
    result.dataCrc32 = 0;
    result.dataBase64 = "";
  }
  return true;
}

RecorderSnapshot LightstickRecorder::snapshot() const {
  RecorderSnapshot value;
  const size_t bufferedRecords = ringUsedRecords();
  value.psramReady = psramReady;
  value.psramBufferBytes = ringCapacity * sizeof(PulseRecord);
  value.psramBufferUsedBytes = bufferedRecords * sizeof(PulseRecord);
  value.usbHostReady = usbHostReady;
  value.usbMounted = usbMounted;
  value.recording = recordingActive;
  value.recordingName = recordingName;
  value.currentFile = currentFilePath;
  value.segmentIndex = currentSegmentIndex;
  value.elapsedMs = recordingActive ? millis() - recordingStartedMs : recordingStoppedElapsedMs;
  value.capturedEdges = capturedEdges;
  value.writtenEdges = writtenEdges;
  value.droppedIsrEdges = droppedIsrEdges;
  value.droppedPsramEdges = droppedPsramEdges;
  value.filteredEdges = filteredEdges;
  value.recordingTarget = recordingTargetName(recordingTarget);
  value.clientRecordingAvailable = clientRecordingAvailable;
  value.clientSessionId = clientSessionId;
  value.clientAcknowledgedRecords = clientAcknowledgedRecords;
  value.clientPendingRecords = clientPendingRecords;
  value.clientBufferedRecords = bufferedRecords;
  value.activeReceiverConfig = activeConfig;
  value.activeRecordingConfig = activeRecordingConfig;
  if (clientRecordingAvailable) {
    const RecordingHeader header = makeRecordingHeader();
    value.clientHeaderBase64 = encodeBase64(
      reinterpret_cast<const uint8_t*>(&header),
      sizeof(header)
    );
  }
  value.stopReason = recordingStopReason;
  value.clientEndOfStream = clientRecordingAvailable && !recordingActive
    && clientPendingRecords == 0 && bufferedRecords == 0;
  value.lastError = lastError;

  if (usbMounted && fileMutex != nullptr
      && xSemaphoreTake(fileMutex, pdMS_TO_TICKS(250)) == pdTRUE) {
    DWORD freeClusters = 0;
    FATFS* filesystem = nullptr;
    if (f_getfree("0:", &freeClusters, &filesystem) == FR_OK && filesystem != nullptr) {
      const uint64_t clusterBytes = static_cast<uint64_t>(filesystem->csize) * filesystem->ssize;
      value.usbTotalBytes = static_cast<uint64_t>(filesystem->n_fatent - 2) * clusterBytes;
      value.usbFreeBytes = static_cast<uint64_t>(freeClusters) * clusterBytes;
    }
    xSemaphoreGive(fileMutex);
  }
  return value;
}

std::vector<RecordingFileInfo> LightstickRecorder::listFiles(String& error) const {
  std::vector<RecordingFileInfo> files;
  if (!usbMounted) {
    error = "FAT32 USB drive is not mounted";
    return files;
  }
  if (fileMutex == nullptr || xSemaphoreTake(fileMutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    error = "recording directory is busy";
    return files;
  }
  if (!usbMounted) {
    error = "FAT32 USB drive is not mounted";
    xSemaphoreGive(fileMutex);
    return files;
  }
  DIR* directory = opendir(RECORDING_DIR);
  if (directory == nullptr) {
    error = String("recording directory open failed: ") + strerror(errno);
    xSemaphoreGive(fileMutex);
    return files;
  }
  while (dirent* entry = readdir(directory)) {
    if (entry->d_name[0] == '.') continue;
    const String path = String(RECORDING_DIR) + "/" + entry->d_name;
    struct stat attributes{};
    if (stat(path.c_str(), &attributes) != 0 || !S_ISREG(attributes.st_mode)) continue;
    RecordingFileInfo file;
    file.name = String(entry->d_name);
    file.sizeBytes = static_cast<uint64_t>(attributes.st_size);
    files.push_back(file);
  }
  closedir(directory);
  xSemaphoreGive(fileMutex);
  return files;
}

bool LightstickRecorder::deleteFile(const String& name, String& error) {
  if (!usbMounted) {
    error = "FAT32 USB drive is not mounted";
    return false;
  }
  if (name.isEmpty() || name.indexOf('/') >= 0 || name.indexOf("..") >= 0) {
    error = "invalid recording file name";
    return false;
  }
  if (fileMutex == nullptr || xSemaphoreTake(fileMutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
    error = "recording directory is busy";
    return false;
  }
  if (!usbMounted) {
    error = "FAT32 USB drive is not mounted";
    xSemaphoreGive(fileMutex);
    return false;
  }
  if (currentFile != nullptr && currentFilePath.endsWith("/" + name)) {
    error = "cannot delete the active recording";
    xSemaphoreGive(fileMutex);
    return false;
  }
  const String path = String(RECORDING_DIR) + "/" + name;
  if (unlink(path.c_str()) != 0) {
    error = String("delete failed: ") + strerror(errno);
    xSemaphoreGive(fileMutex);
    return false;
  }
  xSemaphoreGive(fileMutex);
  return true;
}
