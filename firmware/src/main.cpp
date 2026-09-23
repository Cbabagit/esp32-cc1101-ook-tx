// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 lightstick-control contributors
#include <Arduino.h>
#include <ArduinoJson.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <SmartRC_CC1101.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include <cstring>
#include <vector>

#include "driver/rmt.h"
#include "esp_err.h"

#include "soc/usb_dwc_struct.h"
#include "soc/usb_wrap_struct.h"
#include "usb/usb_host.h"

#include "recorder.h"

namespace {

constexpr uint8_t PIN_SCK = 4;
constexpr uint8_t PIN_MOSI = 5;
constexpr uint8_t PIN_MISO = 6;
constexpr uint8_t PIN_CSN = 7;
constexpr uint8_t PIN_GDO0 = 15;
constexpr uint8_t PIN_GDO2 = 16;

constexpr uint8_t REG_PARTNUM = 0x30;
constexpr uint8_t REG_VERSION = 0x31;
constexpr uint8_t REG_MARCSTATE = 0x35;
constexpr uint8_t REG_TXBYTES = 0x3A;
constexpr uint8_t REG_RXBYTES = 0x3B;
constexpr uint8_t FIFO_ADDRESS = 0x3F;

constexpr uint8_t MARCSTATE_IDLE = 0x01;
constexpr uint8_t MARCSTATE_RX = 0x0D;
constexpr uint8_t MARCSTATE_TX = 0x13;
constexpr uint8_t MARCSTATE_TXFIFO_UNDERFLOW = 0x16;
constexpr uint8_t STROBE_SCAL = 0x33;
constexpr uint8_t STROBE_STX = 0x35;
constexpr uint8_t STROBE_SIDLE = 0x36;
constexpr uint8_t STROBE_SFTX = 0x3B;

constexpr size_t MAX_SERIAL_LINE = 24576;
constexpr size_t MAX_PULSES = 2048;
constexpr uint32_t MAX_PULSE_US = 100000;
constexpr uint64_t MAX_SEQUENCE_US = 1000000;
constexpr uint8_t MAX_REPEAT = 10;
constexpr float RMT_TICK_NS = 1000.0f;
constexpr uint8_t RMT_CLOCK_DIV = 80;
constexpr uint16_t RMT_MAX_HALF_TICKS = 32767;
constexpr uint32_t RMT_TX_TIMEOUT_MARGIN_US = 10000;
// ESP-IDF's legacy RMT driver logs "Timeout on wait_tx_done" on every poll
// slice that expires. A 1 ms poll therefore drowns the 921600-baud console
// with one line per millisecond of waveform, which steals bandwidth from the
// command stream. Waiting in larger slices keeps ABORT latency bounded
// without the log storm.
constexpr TickType_t RMT_TX_POLL_TICKS = pdMS_TO_TICKS(1);
constexpr TickType_t RMT_TX_WAIT_SLICE_TICKS = pdMS_TO_TICKS(50);
constexpr rmt_channel_t PULSE_RMT_CHANNEL = RMT_CHANNEL_0;
constexpr size_t MAX_CACHED_TX_RESULTS = 4;
constexpr size_t MAX_SIGNAL_PROFILES = 16;
constexpr size_t MAX_SIGNAL_NAME = 24;
constexpr size_t MAX_SIGNAL_DURATIONS = 400;
constexpr size_t MAX_SIGNAL_DATA = 61;
constexpr char FIRMWARE_VERSION[] = "0.5.2";

// Public source builds intentionally contain no private network credentials.
constexpr char WIFI_STA_SSID[] = "";
constexpr char WIFI_STA_PASSWORD[] = "";
constexpr char WIFI_HOSTNAME[] = "lightstick-n16r8";
constexpr char WIFI_RECOVERY_SSID[] = "Lightstick-Recovery";
constexpr uint32_t WIFI_RECOVERY_DELAY_MS = 30000;
constexpr uint32_t WIFI_RECONNECT_INTERVAL_MS = 10000;
constexpr uint16_t DISCOVERY_PORT = 4210;
constexpr char DISCOVERY_REQUEST[] = "LIGHTSTICK_DISCOVER";
// 同一个 UDP 端口既做发现也收命令: 收到 LIGHTSTICK_DISCOVER 就回设备信息,
// 收到以 { 开头的就当 JSON 命令跑 (和串口/BLE 走同一套处理)。
// 单包上限压到 1400, 避免 IP 分片; 超长命令请走串口或 HTTP。
constexpr size_t UDP_MAX_COMMAND = 1400;
constexpr char BLE_DEVICE_NAME[] = "Lightstick N16R8";
constexpr char BLE_SERVICE_UUID[] = "8f7a0001-4c53-4331-9638-53334e313652";
constexpr char BLE_COMMAND_UUID[] = "8f7a0002-4c53-4331-9638-53334e313652";
constexpr char BLE_RESPONSE_UUID[] = "8f7a0003-4c53-4331-9638-53334e313652";
constexpr size_t BLE_NOTIFY_CHUNK = 180;
constexpr bool BLE_ENABLED = true;

SmartRC_CC1101 radio;
String serialLine;
SemaphoreHandle_t serialMutex = nullptr;
SemaphoreHandle_t commandMutex = nullptr;
SemaphoreHandle_t txResultMutex = nullptr;
SemaphoreHandle_t pulseRmtMutex = nullptr;
QueueHandle_t pulseQueue = nullptr;
QueueHandle_t bleCommandQueue = nullptr;
volatile bool abortRequested = false;
volatile bool txBusy = false;
bool pulseRmtInstalled = false;
bool radioFound = false;
bool radioCalibrated = false;
uint8_t radioCalibrationMarcState = 0;
String radioCalibrationError;
ReceiverConfig receiverConfig;
RecordingConfig recordingConfig;
Preferences preferences;
WebServer webServer(80);
WiFiUDP discoveryUdp;
BLECharacteristic* bleResponseCharacteristic = nullptr;
String bleInput;
bool bleConnected = false;
String* responseSink = nullptr;
String bootId;
uint32_t wifiStartedAtMs = 0;
uint32_t wifiLastReconnectMs = 0;
uint32_t wifiReconnectCount = 0;
bool recoveryApActive = false;
bool mdnsActive = false;
bool discoveryActive = false;
IPAddress udpPeerIp;          // 最近一次 UDP 命令的来源, 异步结果回它
uint16_t udpPeerPort = 0;
bool udpPeerActive = false;
bool wifiWasConnected = false;
String wifiSsid = WIFI_STA_SSID;
String wifiPassword = WIFI_STA_PASSWORD;

struct SignalProfile {
  String name;
  String kind;
  uint32_t frequencyHz = 433920000;
  int8_t powerDbm = -30;
  uint8_t repeat = 1;
  uint32_t gapUs = 20000;
  uint8_t startLevel = 1;
  std::vector<uint32_t> durationsUs;
  std::vector<uint8_t> data;
  ReceiverConfig receiver;
  RecordingConfig recording;
  int storageSlot = -1;
};

std::vector<SignalProfile> signalProfiles;

struct __attribute__((packed)) PersistedSignalProfile {
  char magic[4];
  uint8_t version;
  char name[MAX_SIGNAL_NAME + 1];
  char kind[12];
  uint32_t frequencyHz;
  int8_t powerDbm;
  uint8_t repeat;
  uint32_t gapUs;
  uint8_t startLevel;
  uint32_t bandwidthHz;
  uint8_t modulation;
  float dataRateKbaud;
  uint8_t edgeMode;
  uint32_t minEdgeIntervalUs;
  uint32_t maxDurationMs;
  uint64_t maxEdges;
  uint16_t clientChunkRecords;
  uint8_t bufferStopThresholdPercent;
  uint8_t stopOnBufferOverflow;
  uint16_t durationCount;
  uint16_t dataCount;
  uint32_t durationsUs[MAX_SIGNAL_DURATIONS];
  uint8_t data[MAX_SIGNAL_DATA];
};

static_assert(sizeof(PersistedSignalProfile) < 2000, "profile must fit in one NVS value");

struct PulseTaskConfig {
  String requestId;
  const char* commandLabel = "TX_PULSES";
  std::vector<uint32_t> durations;
  uint32_t frequencyHz;
  uint32_t gapUs;
  int8_t powerDbm;
  uint8_t repeat;
  uint8_t startLevel;
};

enum class TxResultState : uint8_t {
  Empty,
  Pending,
  Success,
  Error,
};

struct TxResultCacheEntry {
  String requestId;
  String command;
  String error;
  TxResultState state = TxResultState::Empty;
  uint32_t sequencesSent = 0;
  uint32_t pulseCount = 0;
  uint32_t frequencyHz = 0;
  uint8_t txState = 0;
};

TxResultCacheEntry txResultCache[MAX_CACHED_TX_RESULTS];
size_t txResultCacheCursor = 0;

void sendBleResponse(const String& response);
void sendUdpResponse(const String& response);

const char* txResultStateName(TxResultState state) {
  switch (state) {
    case TxResultState::Pending:
      return "pending";
    case TxResultState::Success:
      return "success";
    case TxResultState::Error:
      return "error";
    case TxResultState::Empty:
    default:
      return "error";
  }
}

TxResultCacheEntry* findTxResultLocked(const String& requestId) {
  for (TxResultCacheEntry& entry : txResultCache) {
    if (entry.state != TxResultState::Empty && entry.requestId == requestId) return &entry;
  }
  return nullptr;
}

void cacheTxPending(const String& requestId, const String& command) {
  if (txResultMutex == nullptr || xSemaphoreTake(txResultMutex, pdMS_TO_TICKS(1000)) != pdTRUE) return;
  TxResultCacheEntry* entry = findTxResultLocked(requestId);
  if (entry == nullptr) {
    entry = &txResultCache[txResultCacheCursor];
    txResultCacheCursor = (txResultCacheCursor + 1) % MAX_CACHED_TX_RESULTS;
  }
  *entry = TxResultCacheEntry();
  entry->requestId = requestId;
  entry->command = command;
  entry->state = TxResultState::Pending;
  xSemaphoreGive(txResultMutex);
}

void cacheTxError(const String& requestId, const String& error, uint32_t frequencyHz = 0, uint8_t txState = 0) {
  if (txResultMutex == nullptr || xSemaphoreTake(txResultMutex, pdMS_TO_TICKS(1000)) != pdTRUE) return;
  TxResultCacheEntry* entry = findTxResultLocked(requestId);
  if (entry == nullptr) {
    entry = &txResultCache[txResultCacheCursor];
    txResultCacheCursor = (txResultCacheCursor + 1) % MAX_CACHED_TX_RESULTS;
    entry->requestId = requestId;
  }
  entry->state = TxResultState::Error;
  entry->error = error;
  entry->frequencyHz = frequencyHz;
  entry->txState = txState;
  xSemaphoreGive(txResultMutex);
}

void cacheTxSuccess(
  const String& requestId,
  const String& command,
  uint32_t sequencesSent,
  uint32_t pulseCount,
  uint32_t frequencyHz,
  uint8_t txState
) {
  if (txResultMutex == nullptr || xSemaphoreTake(txResultMutex, pdMS_TO_TICKS(1000)) != pdTRUE) return;
  TxResultCacheEntry* entry = findTxResultLocked(requestId);
  if (entry == nullptr) {
    entry = &txResultCache[txResultCacheCursor];
    txResultCacheCursor = (txResultCacheCursor + 1) % MAX_CACHED_TX_RESULTS;
    entry->requestId = requestId;
  }
  entry->command = command;
  entry->state = TxResultState::Success;
  entry->error = "";
  entry->sequencesSent = sequencesSent;
  entry->pulseCount = pulseCount;
  entry->frequencyHz = frequencyHz;
  entry->txState = txState;
  xSemaphoreGive(txResultMutex);
}

bool addCachedTxResult(const String& requestId, JsonDocument& result) {
  if (txResultMutex == nullptr || xSemaphoreTake(txResultMutex, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
  const TxResultCacheEntry* entry = findTxResultLocked(requestId);
  if (entry == nullptr) {
    xSemaphoreGive(txResultMutex);
    return false;
  }
  result["request_id"] = entry->requestId;
  result["status"] = txResultStateName(entry->state);
  result["command"] = entry->command;
  if (!entry->error.isEmpty()) result["error"] = entry->error;
  if (entry->state != TxResultState::Pending) {
    result["sequences_sent"] = entry->sequencesSent;
    result["frequency_hz"] = entry->frequencyHz;
    result["tx_state"] = entry->txState;
    if (entry->command == "TX_PULSES") result["pulse_count"] = entry->pulseCount;
  }
  xSemaphoreGive(txResultMutex);
  return true;
}

void writeSerialJson(const JsonDocument& document) {
  if (serialMutex != nullptr && xSemaphoreTake(serialMutex, pdMS_TO_TICKS(500)) == pdTRUE) {
    serializeJson(document, Serial);
    Serial.write('\n');
    Serial.flush();
    xSemaphoreGive(serialMutex);
  }
}

void writeJson(const JsonDocument& document) {
  if (responseSink != nullptr) {
    responseSink->remove(0);
    serializeJson(document, *responseSink);
    return;
  }
  writeSerialJson(document);
}

void respondAsyncError(const String& id, const String& error) {
  JsonDocument response;
  response["id"] = id;
  response["ok"] = false;
  response["event"] = "TX_COMPLETE";
  response["error"] = error;
  writeSerialJson(response);
  String bleResponse;
  serializeJson(response, bleResponse);
  sendBleResponse(bleResponse);
  sendUdpResponse(bleResponse);
}

void respondAsyncOk(const String& id, const JsonDocument& result) {
  JsonDocument response;
  response["id"] = id;
  response["ok"] = true;
  response["event"] = "TX_COMPLETE";
  response["result"].set(result);
  writeSerialJson(response);
  String bleResponse;
  serializeJson(response, bleResponse);
  sendBleResponse(bleResponse);
  sendUdpResponse(bleResponse);
}

void respondError(const String& id, const String& error) {
  JsonDocument response;
  response["id"] = id;
  response["ok"] = false;
  response["error"] = error;
  writeJson(response);
}

void respondOk(const String& id, const JsonDocument& result) {
  JsonDocument response;
  response["id"] = id;
  response["ok"] = true;
  response["result"].set(result);
  writeJson(response);
}

void respondSimple(const String& id, const char* status = "ok") {
  JsonDocument result;
  result["status"] = status;
  respondOk(id, result);
}

bool parseHex(const String& input, std::vector<uint8_t>& output, String& error) {
  String cleaned;
  cleaned.reserve(input.length());
  for (char character : input) {
    if (isxdigit(static_cast<unsigned char>(character))) cleaned += character;
  }
  if (cleaned.length() % 2 != 0) {
    error = "hex must contain complete bytes";
    return false;
  }
  output.clear();
  output.reserve(cleaned.length() / 2);
  for (size_t index = 0; index < cleaned.length(); index += 2) {
    char pair[3] = {cleaned[index], cleaned[index + 1], '\0'};
    output.push_back(static_cast<uint8_t>(strtoul(pair, nullptr, 16)));
  }
  return true;
}

String bytesToHex(const uint8_t* bytes, size_t length) {
  static const char* digits = "0123456789ABCDEF";
  String output;
  output.reserve(length * 3);
  for (size_t index = 0; index < length; ++index) {
    if (index != 0) output += ' ';
    output += digits[bytes[index] >> 4];
    output += digits[bytes[index] & 0x0F];
  }
  return output;
}

void idleRadio() {
  radio.setSidle();
  pinMode(PIN_GDO0, INPUT);
}

uint8_t readMarcState() {
  return radio.SpiReadStatus(REG_MARCSTATE) & 0x1F;
}

bool waitForMarcState(uint8_t expected, uint32_t timeoutMs, uint8_t& observed) {
  const uint32_t startedAt = millis();
  do {
    observed = readMarcState();
    if (observed == expected) return true;
    if (observed == MARCSTATE_TXFIFO_UNDERFLOW) return false;
    delayMicroseconds(100);
  } while (millis() - startedAt < timeoutMs);
  return false;
}

bool delayAbortable(uint32_t durationUs) {
  while (durationUs > 0) {
    if (abortRequested) return false;
    const uint32_t chunkUs = min(durationUs, static_cast<uint32_t>(1000));
    delayMicroseconds(chunkUs);
    durationUs -= chunkUs;
  }
  return !abortRequested;
}

void configurePulseTx(uint32_t frequencyHz, int8_t powerDbm) {
  radioCalibrated = false;
  radio.setSidle();
  radio.setCCMode(false);
  radio.setModulation(2);
  radio.setMHZ(static_cast<float>(frequencyHz) / 1000000.0f);
  radio.setDRate(2.0f);
  radio.setPA(constrain(powerDbm, -30, 10));
  radio.setSyncMode(0);
  radio.setPktFormat(3);
  radio.setCrc(false);
  pinMode(PIN_GDO0, OUTPUT);
  digitalWrite(PIN_GDO0, LOW);
}

String pulseRmtError(const char* action, esp_err_t status) {
  return String(action) + ": " + esp_err_to_name(status);
}

bool stopPulseRmt(String& error) {
  error = "";
  if (pulseRmtMutex == nullptr) return true;
  xSemaphoreTake(pulseRmtMutex, portMAX_DELAY);
  if (!pulseRmtInstalled) {
    xSemaphoreGive(pulseRmtMutex);
    return true;
  }
  const esp_err_t status = rmt_tx_stop(PULSE_RMT_CHANNEL);
  xSemaphoreGive(pulseRmtMutex);
  if (status != ESP_OK) {
    error = pulseRmtError("failed to stop ESP32-S3 RMT TX", status);
    return false;
  }
  return true;
}

bool cleanupPulseRmt(String& error) {
  error = "";
  bool success = true;
  if (pulseRmtMutex != nullptr) {
    xSemaphoreTake(pulseRmtMutex, portMAX_DELAY);
    if (pulseRmtInstalled) {
      const esp_err_t stopStatus = rmt_tx_stop(PULSE_RMT_CHANNEL);
      if (stopStatus != ESP_OK) {
        error = pulseRmtError("failed to stop ESP32-S3 RMT TX", stopStatus);
        success = false;
      }
      const esp_err_t uninstallStatus = rmt_driver_uninstall(PULSE_RMT_CHANNEL);
      if (uninstallStatus != ESP_OK) {
        const String detail = pulseRmtError("failed to uninstall ESP32-S3 RMT TX", uninstallStatus);
        if (!error.isEmpty()) error += "; ";
        error += detail;
        success = false;
      } else {
        pulseRmtInstalled = false;
      }
    }
    xSemaphoreGive(pulseRmtMutex);
  }
  idleRadio();
  return success;
}

bool beginPulseRmt(float& actualTickNs, String& error) {
  actualTickNs = 0.0f;
  error = "";
  if (pulseRmtMutex == nullptr) {
    error = "RMT mutex is unavailable";
    idleRadio();
    return false;
  }

  xSemaphoreTake(pulseRmtMutex, portMAX_DELAY);
  if (pulseRmtInstalled) {
    xSemaphoreGive(pulseRmtMutex);
    error = "ESP32-S3 RMT TX is already installed";
    idleRadio();
    return false;
  }

  rmt_config_t config = RMT_DEFAULT_CONFIG_TX(static_cast<gpio_num_t>(PIN_GDO0), PULSE_RMT_CHANNEL);
  config.clk_div = RMT_CLOCK_DIV;
  config.mem_block_num = 4;  // proven-good RMT_MEM_256 capacity
  config.tx_config.carrier_en = false;
  config.tx_config.idle_output_en = true;
  config.tx_config.idle_level = RMT_IDLE_LEVEL_LOW;

  esp_err_t status = rmt_config(&config);
  bool driverInstalled = false;
  if (status == ESP_OK) {
    status = rmt_driver_install(PULSE_RMT_CHANNEL, 0, 0);
    driverInstalled = status == ESP_OK;
  }
  if (status == ESP_OK) status = rmt_set_source_clk(PULSE_RMT_CHANNEL, RMT_BASECLK_APB);
  if (status == ESP_OK) status = rmt_set_clk_div(PULSE_RMT_CHANNEL, RMT_CLOCK_DIV);

  if (status != ESP_OK) {
    error = pulseRmtError("failed to configure ESP32-S3 RMT TX", status);
    if (driverInstalled) {
      const esp_err_t stopStatus = rmt_tx_stop(PULSE_RMT_CHANNEL);
      if (stopStatus != ESP_OK) {
        error += String("; ") + pulseRmtError("failed to stop ESP32-S3 RMT TX", stopStatus);
      }
      const esp_err_t uninstallStatus = rmt_driver_uninstall(PULSE_RMT_CHANNEL);
      if (uninstallStatus != ESP_OK) {
        error += String("; ") + pulseRmtError("failed to uninstall ESP32-S3 RMT TX", uninstallStatus);
        pulseRmtInstalled = true;
      }
    }
    xSemaphoreGive(pulseRmtMutex);
    idleRadio();
    return false;
  }

  pulseRmtInstalled = true;
  actualTickNs = RMT_TICK_NS;
  xSemaphoreGive(pulseRmtMutex);
  return true;
}

bool startPulseRmt(const std::vector<rmt_item32_t>& waveform, String& error) {
  error = "";
  if (waveform.empty()) {
    error = "RMT waveform is empty";
    return false;
  }
  if (pulseRmtMutex == nullptr) {
    error = "RMT mutex is unavailable";
    return false;
  }
  xSemaphoreTake(pulseRmtMutex, portMAX_DELAY);
  if (!pulseRmtInstalled) {
    xSemaphoreGive(pulseRmtMutex);
    error = "ESP32-S3 RMT TX is not installed";
    return false;
  }
  const esp_err_t status = rmt_write_items(
    PULSE_RMT_CHANNEL,
    waveform.data(),
    static_cast<int>(waveform.size()),
    false
  );
  xSemaphoreGive(pulseRmtMutex);
  if (status != ESP_OK) {
    error = pulseRmtError("failed to start ESP32-S3 RMT pulse output", status);
    return false;
  }
  return true;
}

void appendPulseRmtHalf(std::vector<rmt_item32_t>& output, uint16_t durationTicks, uint8_t level) {
  if (output.empty() || output.back().duration1 != 0) {
    rmt_item32_t item{};
    item.duration0 = durationTicks;
    item.level0 = level;
    item.level1 = level;
    output.push_back(item);
  } else {
    output.back().duration1 = durationTicks;
    output.back().level1 = level;
  }
}

bool buildPulseRmtWaveform(
  const PulseTaskConfig& config,
  float tickNs,
  std::vector<rmt_item32_t>& output,
  uint64_t& totalTicks
) {
  output.clear();
  output.reserve(config.durations.size() + 32);
  totalTicks = 0;
  uint8_t level = config.startLevel;
  for (const uint32_t durationUs : config.durations) {
    const uint64_t durationTicks = max<uint64_t>(
      1,
      static_cast<uint64_t>(durationUs * 1000.0f / tickNs + 0.5f)
    );
    totalTicks += durationTicks;
    uint64_t remainingTicks = durationTicks;
    while (remainingTicks > 0) {
      const uint16_t chunkTicks = static_cast<uint16_t>(min<uint64_t>(
        remainingTicks,
        RMT_MAX_HALF_TICKS
      ));
      // A long input duration is split into same-level half-items, so no edge
      // is inserted at the RMT memory boundary or at a 32767-tick boundary.
      appendPulseRmtHalf(output, chunkTicks, level);
      remainingTicks -= chunkTicks;
    }
    level ^= 1;
  }
  return !output.empty() && totalTicks > 0;
}

bool waitForPulseRmt(uint64_t totalTicks, float tickNs, String& error) {
  error = "";
  const uint64_t expectedUs = static_cast<uint64_t>(totalTicks * tickNs / 1000.0f + 0.5f);
  const uint32_t timeoutUs = static_cast<uint32_t>(min<uint64_t>(
    MAX_SEQUENCE_US + RMT_TX_TIMEOUT_MARGIN_US,
    expectedUs + RMT_TX_TIMEOUT_MARGIN_US
  ));
  const uint32_t startedAt = micros();
  while (!abortRequested) {
    const esp_err_t status = rmt_wait_tx_done(PULSE_RMT_CHANNEL, RMT_TX_WAIT_SLICE_TICKS);
    if (status == ESP_OK) return true;
    if (status == ESP_ERR_TIMEOUT) {
      if (static_cast<uint32_t>(micros() - startedAt) < timeoutUs) continue;
      String stopError;
      stopPulseRmt(stopError);
      error = "ESP32-S3 RMT TX exceeded the expected waveform duration";
      if (!stopError.isEmpty()) error += String("; ") + stopError;
      return false;
    }
    if (abortRequested) break;
    error = pulseRmtError("failed while waiting for ESP32-S3 RMT TX", status);
    return false;
  }
  String stopError;
  if (!stopPulseRmt(stopError) && error.isEmpty()) error = stopError;
  return false;
}

void configurePacketMode(uint32_t frequencyHz, int8_t powerDbm) {
  radioCalibrated = false;
  radio.setSidle();
  radio.setCCMode(true);
  radio.setModulation(2);
  radio.setMHZ(static_cast<float>(frequencyHz) / 1000000.0f);
  radio.setDRate(2.0f);
  radio.setPA(constrain(powerDbm, -30, 10));
  radio.setSyncMode(0);
  radio.setPktFormat(0);
  radio.setLengthConfig(1);
  radio.setCrc(false);
  radio.setWhiteData(false);
  pinMode(PIN_GDO0, INPUT);
}

bool validCc1101Frequency(uint32_t frequencyHz) {
  return (frequencyHz >= 300000000UL && frequencyHz <= 348000000UL)
    || (frequencyHz >= 378000000UL && frequencyHz <= 464000000UL)
    || (frequencyHz >= 779000000UL && frequencyHz <= 928000000UL);
}

void configureAsyncReceive(const ReceiverConfig& config) {
  radioCalibrated = false;
  radio.setSidle();
  radio.setCCMode(false);
  radio.setModulation(config.modulation);
  radio.setMHZ(static_cast<float>(config.frequencyHz) / 1000000.0f);
  radio.setRxBW(static_cast<float>(config.bandwidthHz) / 1000.0f);
  radio.setDRate(config.dataRateKbaud);
  radio.setSyncMode(0);
  radio.setPktFormat(3);
  radio.setCrc(false);
  pinMode(PIN_GDO0, INPUT);
}

bool calibrateRadio(String& error) {
  if (!radioFound) {
    error = "cc1101 not found";
    radioCalibrated = false;
    radioCalibrationError = error;
    return false;
  }
  radio.setSidle();
  radio.SpiStrobe(STROBE_SCAL);
  delay(2);
  uint8_t observed = 0;
  if (!waitForMarcState(MARCSTATE_IDLE, 50, observed)) {
    error = "cc1101 calibration did not return to IDLE";
    radioCalibrated = false;
    radioCalibrationMarcState = observed;
    radioCalibrationError = error;
    return false;
  }
  radioCalibrated = true;
  radioCalibrationMarcState = observed;
  radioCalibrationError = "";
  return true;
}

bool prepareAsyncReceiver(ReceiverConfig& config, String& error) {
  configureAsyncReceive(config);
  if (!calibrateRadio(error)) {
    idleRadio();
    return false;
  }
  config.frequencyHz = static_cast<uint32_t>(radio.getMHZ() * 1000000.0f + 0.5f);
  config.bandwidthHz = static_cast<uint32_t>(radio.getRxBW() * 1000.0f + 0.5f);
  config.dataRateKbaud = radio.getDRate();
  return true;
}

bool prepareAsyncReceiver(String& error) {
  return prepareAsyncReceiver(receiverConfig, error);
}

bool resetAndPrepareRadio(String& error) {
  idleRadio();
  radioCalibrated = false;
  radioCalibrationError = "";
  radio.setSres();
  delay(2);
  radio.Init();
  radioFound = radio.getCC1101();
  if (!radioFound) {
    error = "cc1101 not found";
    radioCalibrationError = error;
    idleRadio();
    return false;
  }
  const bool prepared = prepareAsyncReceiver(error);
  idleRadio();
  return prepared;
}

const char* recordingEdgeModeName(RecordingEdgeMode mode) {
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

bool parseRecordingEdgeMode(const String& value, RecordingEdgeMode& mode) {
  if (value == "both") {
    mode = RecordingEdgeMode::Both;
    return true;
  }
  if (value == "rising") {
    mode = RecordingEdgeMode::Rising;
    return true;
  }
  if (value == "falling") {
    mode = RecordingEdgeMode::Falling;
    return true;
  }
  return false;
}

void persistReceiverConfig() {
  preferences.putULong("freq", receiverConfig.frequencyHz);
  preferences.putULong("bw", receiverConfig.bandwidthHz);
  preferences.putUChar("mod", receiverConfig.modulation);
  preferences.putFloat("rate", receiverConfig.dataRateKbaud);
}

void persistRecordingConfig() {
  preferences.putUChar("edge", static_cast<uint8_t>(recordingConfig.edgeMode));
  preferences.putUInt("edge_min", recordingConfig.minEdgeIntervalUs);
  preferences.putUInt("duration", recordingConfig.maxDurationMs);
  preferences.putULong64("max_edges", recordingConfig.maxEdges);
  preferences.putUShort("chunk", recordingConfig.clientChunkRecords);
  preferences.putUChar("buf_stop", recordingConfig.bufferStopThresholdPercent);
  preferences.putBool("stop_ovf", recordingConfig.stopOnBufferOverflow);
}

void loadReceiverConfig() {
  receiverConfig.frequencyHz = preferences.getULong("freq", 433920000UL);
  receiverConfig.bandwidthHz = preferences.getULong("bw", 203000UL);
  receiverConfig.modulation = preferences.getUChar("mod", 2);
  receiverConfig.dataRateKbaud = preferences.getFloat("rate", 4.8f);
  if (!validCc1101Frequency(receiverConfig.frequencyHz)) receiverConfig.frequencyHz = 433920000UL;
  receiverConfig.bandwidthHz = constrain(receiverConfig.bandwidthHz, 58000UL, 812500UL);
  receiverConfig.modulation = min(receiverConfig.modulation, static_cast<uint8_t>(4));
  receiverConfig.dataRateKbaud = constrain(receiverConfig.dataRateKbaud, 0.6f, 500.0f);
}

void loadRecordingConfig() {
  recordingConfig.edgeMode = static_cast<RecordingEdgeMode>(preferences.getUChar("edge", 0));
  recordingConfig.minEdgeIntervalUs = preferences.getUInt("edge_min", 0);
  recordingConfig.maxDurationMs = preferences.getUInt("duration", 0);
  recordingConfig.maxEdges = preferences.getULong64("max_edges", 0);
  recordingConfig.clientChunkRecords = preferences.getUShort("chunk", 512);
  recordingConfig.bufferStopThresholdPercent = preferences.getUChar("buf_stop", 90);
  recordingConfig.stopOnBufferOverflow = preferences.getBool("stop_ovf", true);
  if (static_cast<uint8_t>(recordingConfig.edgeMode) > 2) {
    recordingConfig.edgeMode = RecordingEdgeMode::Both;
  }
  recordingConfig.minEdgeIntervalUs = min<uint32_t>(recordingConfig.minEdgeIntervalUs, 1000000U);
  recordingConfig.maxDurationMs = min<uint32_t>(recordingConfig.maxDurationMs, 86400000U);
  recordingConfig.clientChunkRecords = constrain(recordingConfig.clientChunkRecords, 1, 512);
  recordingConfig.bufferStopThresholdPercent = constrain(
    recordingConfig.bufferStopThresholdPercent,
    static_cast<uint8_t>(50),
    static_cast<uint8_t>(95)
  );
}

void persistWifiConfig() {
  preferences.putString("wifi_ssid", wifiSsid);
  preferences.putString("wifi_pass", wifiPassword);
}

void loadWifiConfig() {
  wifiSsid = preferences.getString("wifi_ssid", WIFI_STA_SSID);
  wifiPassword = preferences.getString("wifi_pass", WIFI_STA_PASSWORD);
}

void addReceiverConfig(JsonObject target) {
  target["frequency_hz"] = receiverConfig.frequencyHz;
  target["rx_bandwidth_hz"] = receiverConfig.bandwidthHz;
  target["modulation"] = receiverConfig.modulation;
  target["data_rate_kbaud"] = receiverConfig.dataRateKbaud;
  target["capture_profile"] = "asynchronous_serial_gdo0";
  target["sync_mode"] = 0;
  target["packet_format"] = 3;
  target["crc_enabled"] = false;
}

void addReceiverConfig(JsonObject target, const ReceiverConfig& config) {
  target["frequency_hz"] = config.frequencyHz;
  target["rx_bandwidth_hz"] = config.bandwidthHz;
  target["modulation"] = config.modulation;
  target["data_rate_kbaud"] = config.dataRateKbaud;
  target["capture_profile"] = "asynchronous_serial_gdo0";
  target["sync_mode"] = 0;
  target["packet_format"] = 3;
  target["crc_enabled"] = false;
}

void addRecordingConfig(JsonObject target, const RecordingConfig& config) {
  target["edge_mode"] = recordingEdgeModeName(config.edgeMode);
  target["min_edge_interval_us"] = config.minEdgeIntervalUs;
  target["max_duration_ms"] = config.maxDurationMs;
  target["max_edges"] = config.maxEdges;
  target["client_chunk_records"] = config.clientChunkRecords;
  target["buffer_stop_threshold_percent"] = config.bufferStopThresholdPercent;
  target["stop_on_buffer_overflow"] = config.stopOnBufferOverflow;
  target["psram_buffer_bytes"] = 4 * 1024 * 1024;
  target["maximum_client_chunk_records"] = 512;
}

bool parseReceiverConfig(JsonObjectConst args, const ReceiverConfig& base, ReceiverConfig& output, String& error) {
  output = base;
  if (args.containsKey("frequency_hz")) output.frequencyHz = args["frequency_hz"].as<uint32_t>();
  if (args.containsKey("rx_bandwidth_hz")) output.bandwidthHz = args["rx_bandwidth_hz"].as<uint32_t>();
  if (args.containsKey("modulation")) output.modulation = args["modulation"].as<uint8_t>();
  if (args.containsKey("data_rate_kbaud")) output.dataRateKbaud = args["data_rate_kbaud"].as<float>();
  if (!validCc1101Frequency(output.frequencyHz)) {
    error = "frequency must be in a CC1101 band: 300-348, 378-464, or 779-928 MHz";
    return false;
  }
  if (output.bandwidthHz < 58000UL || output.bandwidthHz > 812500UL) {
    error = "rx_bandwidth_hz must be in 58000..812500";
    return false;
  }
  if (output.modulation > 4) {
    error = "modulation must be 0=2FSK, 1=GFSK, 2=ASK/OOK, 3=4FSK, or 4=MSK";
    return false;
  }
  if (output.dataRateKbaud < 0.6f || output.dataRateKbaud > 500.0f) {
    error = "data_rate_kbaud must be in 0.6..500";
    return false;
  }
  return true;
}

bool parseRecordingConfig(JsonObjectConst args, const RecordingConfig& base, RecordingConfig& output, String& error) {
  output = base;
  if (args.containsKey("edge_mode")) {
    if (!parseRecordingEdgeMode(String(args["edge_mode"].as<const char*>()), output.edgeMode)) {
      error = "edge_mode must be both, rising, or falling";
      return false;
    }
  }
  if (args.containsKey("min_edge_interval_us")) {
    output.minEdgeIntervalUs = args["min_edge_interval_us"].as<uint32_t>();
  }
  if (args.containsKey("max_duration_ms")) output.maxDurationMs = args["max_duration_ms"].as<uint32_t>();
  if (args.containsKey("max_edges")) output.maxEdges = args["max_edges"].as<uint64_t>();
  if (args.containsKey("client_chunk_records")) {
    output.clientChunkRecords = args["client_chunk_records"].as<uint16_t>();
  }
  if (args.containsKey("buffer_stop_threshold_percent")) {
    output.bufferStopThresholdPercent = args["buffer_stop_threshold_percent"].as<uint8_t>();
  }
  if (args.containsKey("stop_on_buffer_overflow")) {
    output.stopOnBufferOverflow = args["stop_on_buffer_overflow"].as<bool>();
  }
  if (output.minEdgeIntervalUs > 1000000UL) {
    error = "min_edge_interval_us must be in 0..1000000";
    return false;
  }
  if (output.maxDurationMs > 24UL * 60 * 60 * 1000) {
    error = "max_duration_ms must be in 0..86400000; zero means unlimited";
    return false;
  }
  if (output.clientChunkRecords < 1 || output.clientChunkRecords > 512) {
    error = "client_chunk_records must be in 1..512";
    return false;
  }
  if (output.bufferStopThresholdPercent < 50 || output.bufferStopThresholdPercent > 95) {
    error = "buffer_stop_threshold_percent must be in 50..95";
    return false;
  }
  return true;
}

bool validSignalProfileName(const String& name, String& error) {
  if (name.isEmpty() || name.length() > MAX_SIGNAL_NAME) {
    error = "profile name must contain 1..24 characters";
    return false;
  }
  for (char character : name) {
    if (!(isalnum(static_cast<unsigned char>(character))
          || character == '-' || character == '_' || character == '.')) {
      error = "profile name may contain only letters, digits, '-', '_', or '.'";
      return false;
    }
  }
  return true;
}

int findSignalProfileIndex(const String& name) {
  for (size_t index = 0; index < signalProfiles.size(); ++index) {
    if (signalProfiles[index].name == name) return static_cast<int>(index);
  }
  return -1;
}

const SignalProfile* findSignalProfile(const String& name) {
  const int index = findSignalProfileIndex(name);
  return index < 0 ? nullptr : &signalProfiles[static_cast<size_t>(index)];
}

bool validateSignalProfile(SignalProfile& profile, String& error) {
  if (!validSignalProfileName(profile.name, error)) return false;
  if (profile.kind.isEmpty() || profile.kind.length() > 11) {
    error = "profile kind must contain 1..11 characters";
    return false;
  }
  if (!validCc1101Frequency(profile.frequencyHz)) {
    error = "profile frequency_hz must be in a CC1101 band: 300-348, 378-464, or 779-928 MHz";
    return false;
  }
  profile.powerDbm = constrain(profile.powerDbm, static_cast<int8_t>(-30), static_cast<int8_t>(10));
  if (profile.repeat < 1 || profile.repeat > MAX_REPEAT) {
    error = "profile repeat must be in 1..10";
    return false;
  }
  profile.gapUs = constrain(profile.gapUs, 1000UL, 1000000UL);
  profile.startLevel = profile.startLevel ? 1 : 0;
  if (profile.durationsUs.size() > MAX_SIGNAL_DURATIONS) {
    error = "profile durations_us requires at most 400 entries";
    return false;
  }
  uint64_t totalUs = 0;
  for (uint32_t& duration : profile.durationsUs) {
    if (duration < 10 || duration > MAX_PULSE_US) {
      error = "profile pulse durations must be in 10..100000 us";
      return false;
    }
    totalUs += duration;
  }
  if (totalUs > MAX_SEQUENCE_US) {
    error = "profile pulse sequence cannot exceed 1000 ms";
    return false;
  }
  if (profile.data.size() > MAX_SIGNAL_DATA) {
    error = "profile data_hex requires at most 61 bytes";
    return false;
  }
  ReceiverConfig receiver;
  if (!parseReceiverConfig(JsonObjectConst(), profile.receiver, receiver, error)) return false;
  profile.receiver = receiver;
  RecordingConfig recording;
  if (!parseRecordingConfig(JsonObjectConst(), profile.recording, recording, error)) return false;
  profile.recording = recording;
  return true;
}

bool parseSignalProfile(JsonObjectConst args, const SignalProfile* base, SignalProfile& output, String& error) {
  output = base == nullptr ? SignalProfile() : *base;
  if (args["name"].is<const char*>()) output.name = String(args["name"] | "");
  if (args["kind"].is<const char*>()) output.kind = String(args["kind"] | "");
  if (args["frequency_hz"].is<uint32_t>()) output.frequencyHz = args["frequency_hz"].as<uint32_t>();
  if (args["power_dbm"].is<int>()) output.powerDbm = args["power_dbm"].as<int8_t>();
  if (args["repeat"].is<uint8_t>()) output.repeat = args["repeat"].as<uint8_t>();
  if (args["gap_us"].is<uint32_t>()) output.gapUs = args["gap_us"].as<uint32_t>();
  if (args["start_level"].is<uint8_t>()) output.startLevel = args["start_level"].as<uint8_t>() ? 1 : 0;

  output.receiver.frequencyHz = output.frequencyHz;
  if (args["receiver"].is<JsonObjectConst>()) {
    ReceiverConfig receiver;
    if (!parseReceiverConfig(args["receiver"].as<JsonObjectConst>(), output.receiver, receiver, error)) {
      return false;
    }
    output.receiver = receiver;
  } else {
    ReceiverConfig receiver;
    if (!parseReceiverConfig(args, output.receiver, receiver, error)) return false;
    output.receiver = receiver;
  }
  output.frequencyHz = output.receiver.frequencyHz;

  if (args["recording"].is<JsonObjectConst>()) {
    RecordingConfig recording;
    if (!parseRecordingConfig(args["recording"].as<JsonObjectConst>(), output.recording, recording, error)) {
      return false;
    }
    output.recording = recording;
  } else {
    RecordingConfig recording;
    if (!parseRecordingConfig(args, output.recording, recording, error)) return false;
    output.recording = recording;
  }

  if (args["durations_us"].is<JsonArrayConst>()) {
    output.durationsUs.clear();
    for (JsonVariantConst value : args["durations_us"].as<JsonArrayConst>()) {
      output.durationsUs.push_back(value.as<uint32_t>());
    }
  }
  if (args["data_hex"].is<const char*>()) {
    if (!parseHex(String(args["data_hex"] | ""), output.data, error)) return false;
  }
  if (output.kind.isEmpty()) {
    output.kind = output.durationsUs.empty() ? (output.data.empty() ? "radio" : "packet") : "pulse";
  }
  return validateSignalProfile(output, error);
}

void profileToStorage(const SignalProfile& profile, PersistedSignalProfile& stored) {
  memset(&stored, 0, sizeof(stored));
  memcpy(stored.magic, "LSP1", 4);
  stored.version = 1;
  strncpy(stored.name, profile.name.c_str(), MAX_SIGNAL_NAME);
  strncpy(stored.kind, profile.kind.c_str(), sizeof(stored.kind) - 1);
  stored.frequencyHz = profile.frequencyHz;
  stored.powerDbm = profile.powerDbm;
  stored.repeat = profile.repeat;
  stored.gapUs = profile.gapUs;
  stored.startLevel = profile.startLevel;
  stored.bandwidthHz = profile.receiver.bandwidthHz;
  stored.modulation = profile.receiver.modulation;
  stored.dataRateKbaud = profile.receiver.dataRateKbaud;
  stored.edgeMode = static_cast<uint8_t>(profile.recording.edgeMode);
  stored.minEdgeIntervalUs = profile.recording.minEdgeIntervalUs;
  stored.maxDurationMs = profile.recording.maxDurationMs;
  stored.maxEdges = profile.recording.maxEdges;
  stored.clientChunkRecords = profile.recording.clientChunkRecords;
  stored.bufferStopThresholdPercent = profile.recording.bufferStopThresholdPercent;
  stored.stopOnBufferOverflow = profile.recording.stopOnBufferOverflow;
  stored.durationCount = static_cast<uint16_t>(profile.durationsUs.size());
  stored.dataCount = static_cast<uint16_t>(profile.data.size());
  for (size_t index = 0; index < profile.durationsUs.size(); ++index) {
    stored.durationsUs[index] = profile.durationsUs[index];
  }
  for (size_t index = 0; index < profile.data.size(); ++index) stored.data[index] = profile.data[index];
}

bool storageToProfile(const PersistedSignalProfile& stored, SignalProfile& profile) {
  if (memcmp(stored.magic, "LSP1", 4) != 0 || stored.version != 1
      || stored.name[0] == '\0' || stored.durationCount > MAX_SIGNAL_DURATIONS
      || stored.dataCount > MAX_SIGNAL_DATA) {
    return false;
  }
  profile = SignalProfile();
  profile.name = String(stored.name);
  profile.kind = String(stored.kind);
  profile.frequencyHz = stored.frequencyHz;
  profile.powerDbm = stored.powerDbm;
  profile.repeat = stored.repeat;
  profile.gapUs = stored.gapUs;
  profile.startLevel = stored.startLevel;
  profile.receiver.frequencyHz = stored.frequencyHz;
  profile.receiver.bandwidthHz = stored.bandwidthHz;
  profile.receiver.modulation = stored.modulation;
  profile.receiver.dataRateKbaud = stored.dataRateKbaud;
  profile.recording.edgeMode = static_cast<RecordingEdgeMode>(stored.edgeMode);
  profile.recording.minEdgeIntervalUs = stored.minEdgeIntervalUs;
  profile.recording.maxDurationMs = stored.maxDurationMs;
  profile.recording.maxEdges = stored.maxEdges;
  profile.recording.clientChunkRecords = stored.clientChunkRecords;
  profile.recording.bufferStopThresholdPercent = stored.bufferStopThresholdPercent;
  profile.recording.stopOnBufferOverflow = stored.stopOnBufferOverflow;
  profile.durationsUs.assign(stored.durationsUs, stored.durationsUs + stored.durationCount);
  profile.data.assign(stored.data, stored.data + stored.dataCount);
  String error;
  return validateSignalProfile(profile, error);
}

String signalProfileStorageKey(size_t slot) {
  return String("p") + String(slot);
}

void loadSignalProfiles() {
  signalProfiles.clear();
  signalProfiles.reserve(MAX_SIGNAL_PROFILES);
  for (size_t slot = 0; slot < MAX_SIGNAL_PROFILES; ++slot) {
    const String key = signalProfileStorageKey(slot);
    const size_t length = preferences.getBytesLength(key.c_str());
    if (length != sizeof(PersistedSignalProfile)) continue;
    PersistedSignalProfile stored{};
    if (preferences.getBytes(key.c_str(), &stored, sizeof(stored)) != sizeof(stored)) continue;
    SignalProfile profile;
    if (storageToProfile(stored, profile) && findSignalProfileIndex(profile.name) < 0) {
      profile.storageSlot = static_cast<int>(slot);
      signalProfiles.push_back(profile);
    }
  }
}

bool saveSignalProfile(const SignalProfile& profile, String& error) {
  int slot = findSignalProfileIndex(profile.name);
  if (slot >= 0) {
    const int existingSlot = signalProfiles[static_cast<size_t>(slot)].storageSlot;
    slot = existingSlot >= 0 ? existingSlot : slot;
  } else {
    for (size_t candidate = 0; candidate < MAX_SIGNAL_PROFILES; ++candidate) {
      const String key = signalProfileStorageKey(candidate);
      if (preferences.getBytesLength(key.c_str()) == 0) {
        slot = static_cast<int>(candidate);
        break;
      }
    }
  }
  if (slot < 0) {
    error = "maximum of 16 signal profiles reached";
    return false;
  }
  PersistedSignalProfile stored{};
  profileToStorage(profile, stored);
  const String key = signalProfileStorageKey(static_cast<size_t>(slot));
  if (preferences.putBytes(key.c_str(), &stored, sizeof(stored)) != sizeof(stored)) {
    error = "failed to persist signal profile";
    return false;
  }
  SignalProfile persisted = profile;
  persisted.storageSlot = slot;
  const int existingIndex = findSignalProfileIndex(profile.name);
  if (existingIndex >= 0) signalProfiles[static_cast<size_t>(existingIndex)] = persisted;
  else signalProfiles.push_back(persisted);
  return true;
}

bool deleteSignalProfile(const String& name, String& error) {
  const int index = findSignalProfileIndex(name);
  if (index < 0) {
    error = "signal profile not found";
    return false;
  }
  const int storageSlot = signalProfiles[static_cast<size_t>(index)].storageSlot;
  if (storageSlot < 0 || storageSlot >= static_cast<int>(MAX_SIGNAL_PROFILES)) {
    error = "signal profile storage slot is invalid";
    return false;
  }
  const String key = signalProfileStorageKey(static_cast<size_t>(storageSlot));
  if (!preferences.remove(key.c_str())) {
    error = "failed to delete signal profile";
    return false;
  }
  signalProfiles.erase(signalProfiles.begin() + index);
  return true;
}

void addSignalProfile(JsonObject target, const SignalProfile& profile) {
  target["name"] = profile.name;
  target["kind"] = profile.kind;
  target["frequency_hz"] = profile.frequencyHz;
  target["power_dbm"] = profile.powerDbm;
  target["repeat"] = profile.repeat;
  target["gap_us"] = profile.gapUs;
  target["start_level"] = profile.startLevel;
  JsonArray durations = target["durations_us"].to<JsonArray>();
  for (uint32_t duration : profile.durationsUs) durations.add(duration);
  if (!profile.data.empty()) target["data_hex"] = bytesToHex(profile.data.data(), profile.data.size());
  JsonObject receiver = target["receiver"].to<JsonObject>();
  addReceiverConfig(receiver, profile.receiver);
  JsonObject recording = target["recording"].to<JsonObject>();
  addRecordingConfig(recording, profile.recording);
}

bool commandSupportsSignalProfile(const String& command) {
  return command == "SET_RX_CONFIG" || command == "SET_RECORDING_CONFIG"
    || command == "SET_CAPTURE_CONFIG" || command == "START_RECORDING"
    || command == "START_CLIENT_RECORDING" || command == "TX_PACKET"
    || command == "TX_PULSES" || command == "TX_D8" || command == "TX_BYTES"
    || command == "RX_PACKET" || command == "RX_PULSES";
}

bool mergeSignalProfileArgs(
  JsonObjectConst source,
  JsonDocument& storage,
  JsonObject& output,
  String& error
) {
  const String profileName = source["profile"].is<const char*>()
    ? String(source["profile"] | "")
    : String(source["profile_name"] | "");
  if (profileName.isEmpty()) {
    storage.set(source);
    output = storage.as<JsonObject>();
    return true;
  }
  const SignalProfile* profile = findSignalProfile(profileName);
  if (profile == nullptr) {
    error = "signal profile not found";
    return false;
  }
  output = storage.to<JsonObject>();
  output["profile"] = profileName;
  output["frequency_hz"] = profile->frequencyHz;
  output["power_dbm"] = profile->powerDbm;
  output["repeat"] = profile->repeat;
  output["gap_us"] = profile->gapUs;
  output["start_level"] = profile->startLevel;
  if (!profile->durationsUs.empty()) {
    JsonArray durations = output["durations_us"].to<JsonArray>();
    for (uint32_t duration : profile->durationsUs) durations.add(duration);
  }
  if (!profile->data.empty()) output["data_hex"] = bytesToHex(profile->data.data(), profile->data.size());
  JsonObject receiver = output["receiver"].to<JsonObject>();
  addReceiverConfig(receiver, profile->receiver);
  JsonObject recording = output["recording"].to<JsonObject>();
  addRecordingConfig(recording, profile->recording);
  for (JsonPairConst pair : source) {
    const String key = pair.key().c_str();
    if (key == "profile" || key == "profile_name") continue;
    if ((key == "receiver" || key == "recording") && pair.value().is<JsonObjectConst>()) {
      JsonObject mergedObject = output[key].to<JsonObject>();
      for (JsonPairConst nested : pair.value().as<JsonObjectConst>()) {
        mergedObject[nested.key()] = nested.value();
      }
      continue;
    }
    output[key] = pair.value();
  }
  if (source["receiver"].is<JsonObjectConst>()) {
    const JsonObjectConst receiverOverride = source["receiver"].as<JsonObjectConst>();
    if (receiverOverride["frequency_hz"].is<uint32_t>()) {
      output["frequency_hz"] = receiverOverride["frequency_hz"];
    }
  }
  return true;
}

void addRecorderStatus(JsonObject target) {
  const RecorderSnapshot snapshot = recorder.snapshot();
  target["psram_ready"] = snapshot.psramReady;
  target["psram_buffer_bytes"] = snapshot.psramBufferBytes;
  target["psram_buffer_used_bytes"] = snapshot.psramBufferUsedBytes;
  target["usb_host_ready"] = snapshot.usbHostReady;
  target["usb_mounted"] = snapshot.usbMounted;
  target["usb_total_bytes"] = snapshot.usbTotalBytes;
  target["usb_free_bytes"] = snapshot.usbFreeBytes;
  target["recording"] = snapshot.recording;
  target["recording_name"] = snapshot.recordingName;
  target["current_file"] = snapshot.currentFile;
  target["segment_index"] = snapshot.segmentIndex;
  target["elapsed_ms"] = snapshot.elapsedMs;
  target["captured_edges"] = snapshot.capturedEdges;
  target["written_edges"] = snapshot.writtenEdges;
  target["dropped_isr_edges"] = snapshot.droppedIsrEdges;
  target["dropped_psram_edges"] = snapshot.droppedPsramEdges;
  target["filtered_edges"] = snapshot.filteredEdges;
  target["recording_target"] = snapshot.recordingTarget;
  target["client_recording_available"] = snapshot.clientRecordingAvailable;
  target["client_end_of_stream"] = snapshot.clientEndOfStream;
  target["client_session_id"] = snapshot.clientSessionId;
  target["client_acknowledged_records"] = snapshot.clientAcknowledgedRecords;
  target["client_pending_records"] = snapshot.clientPendingRecords;
  target["client_buffered_records"] = snapshot.clientBufferedRecords;
  target["stop_reason"] = snapshot.stopReason;
  JsonObject activeReceive = target["active_receiver"].to<JsonObject>();
  addReceiverConfig(activeReceive, snapshot.activeReceiverConfig);
  JsonObject activeRecording = target["active_recording_config"].to<JsonObject>();
  addRecordingConfig(activeRecording, snapshot.activeRecordingConfig);
  target["last_error"] = snapshot.lastError;
}

const char* wifiStatusName(wl_status_t status) {
  switch (status) {
    case WL_CONNECTED:
      return "connected";
    case WL_NO_SSID_AVAIL:
      return "ssid_unavailable";
    case WL_CONNECT_FAILED:
      return "connect_failed";
    case WL_CONNECTION_LOST:
      return "connection_lost";
    case WL_DISCONNECTED:
      return "disconnected";
    case WL_IDLE_STATUS:
    default:
      return "connecting";
  }
}

void requestWifiConnection() {
  if (recoveryApActive) {
    WiFi.softAPdisconnect(true);
    recoveryApActive = false;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(WIFI_HOSTNAME);
  // Keep modem sleep enabled; ESP-IDF 4.4 BLE/Wi-Fi coexistence requires it.
  WiFi.setAutoReconnect(true);
  WiFi.disconnect(false, false);
  wifiStartedAtMs = millis();
  wifiLastReconnectMs = wifiStartedAtMs;
  wifiWasConnected = false;
  if (wifiSsid.isEmpty()) return;
  if (wifiPassword.isEmpty()) WiFi.begin(wifiSsid.c_str());
  else WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
}

void addWifiStatus(JsonObject target) {
  const wl_status_t status = WiFi.status();
  const bool connected = status == WL_CONNECTED;
  target["mode"] = recoveryApActive ? "sta_with_recovery_ap" : "sta";
  target["ssid"] = wifiSsid;
  target["status"] = wifiStatusName(status);
  target["status_code"] = static_cast<int>(status);
  target["connected"] = connected;
  target["ip"] = connected ? WiFi.localIP().toString() : "0.0.0.0";
  target["gateway"] = connected ? WiFi.gatewayIP().toString() : "0.0.0.0";
  target["subnet_mask"] = connected ? WiFi.subnetMask().toString() : "0.0.0.0";
  target["rssi_dbm"] = connected ? WiFi.RSSI() : 0;
  target["mac"] = WiFi.macAddress();
  target["hostname"] = WIFI_HOSTNAME;
  target["mdns"] = String(WIFI_HOSTNAME) + ".local";
  target["http_port"] = 80;
  target["discovery_udp_port"] = DISCOVERY_PORT;
  target["reconnect_count"] = wifiReconnectCount;
  target["recovery_ap_active"] = recoveryApActive;
  if (recoveryApActive) {
    target["recovery_ap_ssid"] = WIFI_RECOVERY_SSID;
    target["recovery_ap_ip"] = WiFi.softAPIP().toString();
  }
}

bool sendPacketOnce(const std::vector<uint8_t>& data, uint8_t& txState, String& error) {
  radio.SpiStrobe(STROBE_SIDLE);
  if (!waitForMarcState(MARCSTATE_IDLE, 20, txState)) {
    error = "cc1101 failed to enter IDLE before packet TX";
    return false;
  }
  radio.SpiStrobe(STROBE_SFTX);
  radio.SpiWriteReg(FIFO_ADDRESS, static_cast<uint8_t>(data.size()));
  radio.SpiWriteBurstReg(FIFO_ADDRESS, const_cast<uint8_t*>(data.data()), data.size());
  radio.SpiStrobe(STROBE_STX);
  if (!waitForMarcState(MARCSTATE_TX, 20, txState)) {
    error = txState == MARCSTATE_TXFIFO_UNDERFLOW
      ? "cc1101 TX FIFO underflow while starting packet"
      : "cc1101 failed to enter TX for packet";
    radio.SpiStrobe(STROBE_SIDLE);
    radio.SpiStrobe(STROBE_SFTX);
    return false;
  }

  const uint32_t completionTimeoutMs = 150 + static_cast<uint32_t>(data.size() + 1) * 4;
  if (!waitForMarcState(MARCSTATE_IDLE, completionTimeoutMs, txState)) {
    error = txState == MARCSTATE_TXFIFO_UNDERFLOW || (radio.SpiReadStatus(REG_TXBYTES) & 0x80)
      ? "cc1101 TX FIFO underflow"
      : "cc1101 packet TX did not complete";
    radio.SpiStrobe(STROBE_SIDLE);
    radio.SpiStrobe(STROBE_SFTX);
    return false;
  }
  radio.SpiStrobe(STROBE_SFTX);
  return true;
}

// ---------------------------------------------------------------------------
//  ██ 特定协议占位符 ██
//
//  下面两个函数原先实现的是一套特定应援棒的空口协议:
//
//    buildD8Frame()         把 9 个 16bit 槽字 + phase 组装成 21 字节帧
//                           (帧头 / 槽字旋转打包 / 校验和)
//    appendFrameDurations() 把帧字节编码成 RMT 时长
//                           (固定前导码 + 每个数据位展开成若干符号 + 游程编码)
//
//  帧布局、前导码内容、逐位展开方式、校验算法都属于该特定协议, 不是通用框架的
//  一部分, 因此本仓库不提供其实现, 只保留函数签名与调用结构作为占位符。
//
//  这样做的后果: 在你补全这两个函数之前, TX_D8 与 TX_BYTES 会把全 0 帧发出去,
//  不会有任何实际意义。要用请先按自己的协议实现它们。
//  作为参考, 通用部分 (参数解析 / 队列 / RMT / CC1101) 都是完整的。
// ---------------------------------------------------------------------------
constexpr uint16_t D8_SLOT_COUNT = 9;      // 槽数, 按你的协议改
constexpr uint16_t D8_FRAME_BYTES = 21;    // 帧长, 按你的协议改
constexpr uint8_t D8_PHASE_SEQUENCE[6] = {0, 0, 0, 0, 0, 0};  // 帧阶段序列, 按你的协议改

int8_t hexNibble(char c) {
  if (c >= (char)48 && c <= (char)57) return static_cast<int8_t>(c - (char)48);
  if (c >= (char)97 && c <= (char)102) return static_cast<int8_t>(c - (char)97 + 10);
  if (c >= (char)65 && c <= (char)70) return static_cast<int8_t>(c - (char)65 + 10);
  return -1;
}

void buildD8Frame(const uint16_t slots[D8_SLOT_COUNT], uint8_t phase, uint8_t out[D8_FRAME_BYTES]) {
  // TODO(协议): 把 slots[] 与 phase 按你的帧格式写进 out[], 末尾补校验。
  (void)slots;
  (void)phase;
  memset(out, 0, D8_FRAME_BYTES);
}

// 通用上限: 单帧最多多少字节 / 展开后最多多少符号。
constexpr size_t MAX_AIR_FRAME_BYTES = 64;
constexpr size_t MAX_AIR_LEVELS = 8192;

void appendFrameDurations(const uint8_t* frame, size_t length, uint32_t bitUs,
                          std::vector<uint32_t>& durations) {
  // TODO(协议): 在这里把你的空口编码写成高低电平时长。
  //   输入: frame[0..length-1] 是一帧字节, bitUs 是符号宽度(微秒)。
  //   输出: 往 durations 里追加交替的"高/低"电平持续时长, 起始电平为高。
  //   参考做法: (1) 先拼出整帧的符号序列(前导码 + 逐位展开),
  //             (2) 再做游程编码, 相邻同电平合并成一段。
  //   注意: durations 的奇偶性必须保持交替, 否则 RMT 会把相邻两个高电平
  //         当成"高 + 低"。
  (void)frame;
  (void)length;
  (void)bitUs;
  (void)durations;
}

void appendD8Durations(const uint8_t frame[D8_FRAME_BYTES], uint32_t bitUs,
                       std::vector<uint32_t>& durations) {
  appendFrameDurations(frame, D8_FRAME_BYTES, bitUs, durations);
}

void executePulseTask(PulseTaskConfig* config) {
  configurePulseTx(config->frequencyHz, config->powerDbm);
  float actualTickNs = 0.0f;
  String pulseError;
  if (!beginPulseRmt(actualTickNs, pulseError)) {
    txBusy = false;
    cacheTxError(config->requestId, pulseError, config->frequencyHz);
    respondAsyncError(config->requestId, pulseError);
    delete config;
    return;
  }
  std::vector<rmt_item32_t> waveform;
  uint64_t totalTicks = 0;
  if (!buildPulseRmtWaveform(*config, actualTickNs, waveform, totalTicks)) {
    String cleanupError;
    cleanupPulseRmt(cleanupError);
    txBusy = false;
    pulseError = "failed to build RMT pulse waveform";
    if (!cleanupError.isEmpty()) pulseError += String("; ") + cleanupError;
    cacheTxError(config->requestId, pulseError, config->frequencyHz);
    respondAsyncError(config->requestId, pulseError);
    delete config;
    return;
  }
  if (abortRequested) {
    String cleanupError;
    cleanupPulseRmt(cleanupError);
    txBusy = false;
    pulseError = "TX aborted before pulse output started";
    if (!cleanupError.isEmpty()) pulseError += String("; ") + cleanupError;
    cacheTxError(config->requestId, pulseError, config->frequencyHz);
    respondAsyncError(config->requestId, pulseError);
    delete config;
    return;
  }
  radio.SetTx();
  uint8_t txState = 0;
  if (!waitForMarcState(MARCSTATE_TX, 20, txState)) {
    String cleanupError;
    cleanupPulseRmt(cleanupError);
    txBusy = false;
    String error = txState == MARCSTATE_TXFIFO_UNDERFLOW
      ? "cc1101 TX FIFO underflow while starting pulse TX"
      : "cc1101 failed to enter TX for pulse output";
    if (!cleanupError.isEmpty()) error += String("; ") + cleanupError;
    cacheTxError(config->requestId, error, config->frequencyHz, txState);
    respondAsyncError(config->requestId, error);
    delete config;
    return;
  }

  bool aborted = false;
  uint32_t sequencesSent = 0;
  for (uint8_t attempt = 0; attempt < config->repeat && !abortRequested; ++attempt) {
    String writeError;
    if (!startPulseRmt(waveform, writeError)) {
      pulseError = writeError;
      break;
    }
    String waitError;
    if (!waitForPulseRmt(totalTicks, actualTickNs, waitError)) {
      if (!waitError.isEmpty()) pulseError = waitError;
      else aborted = true;
      break;
    }
    // RMT's configured idle output is low. Stop the channel before the gap so
    // the CC1101 stays in TX while no carrier is driven on GDO0.
    String stopError;
    if (!stopPulseRmt(stopError)) {
      pulseError = stopError;
      break;
    }
    ++sequencesSent;
    if (attempt + 1 < config->repeat && !abortRequested
        && !delayAbortable(config->gapUs)) {
      aborted = true;
    }
  }
  aborted = aborted || abortRequested;
  String cleanupError;
  if (!cleanupPulseRmt(cleanupError) && pulseError.isEmpty()) pulseError = cleanupError;
  txBusy = false;

  if (!pulseError.isEmpty()) {
    cacheTxError(config->requestId, pulseError, config->frequencyHz, txState);
    respondAsyncError(config->requestId, pulseError);
  } else if (aborted) {
    const String error = "TX aborted before all requested sequences completed";
    cacheTxError(config->requestId, error, config->frequencyHz, txState);
    respondAsyncError(config->requestId, error);
  } else {
    cacheTxSuccess(
      config->requestId,
      config->commandLabel,
      sequencesSent,
      config->durations.size(),
      config->frequencyHz,
      txState
    );
    JsonDocument result;
    result["status"] = "success";
    result["request_id"] = config->requestId;
    result["command"] = config->commandLabel;
    result["sequences_sent"] = sequencesSent;
    result["pulse_count"] = config->durations.size();
    result["frequency_hz"] = config->frequencyHz;
    result["tx_state"] = txState;
    result["tx_started"] = true;
    respondAsyncOk(config->requestId, result);
  }
  delete config;
}

void pulseTask(void*) {
  while (true) {
    PulseTaskConfig* config = nullptr;
    if (xQueueReceive(pulseQueue, &config, portMAX_DELAY) == pdTRUE && config != nullptr) {
      executePulseTask(config);
    }
  }
}

void handleGetInfo(const String& id) {
  JsonDocument result;
  result["firmware"] = "lightstick-bridge";
  result["version"] = FIRMWARE_VERSION;
  result["boot_id"] = bootId;
  result["hardware_profile"] = "ESP32-S3-N16R8";
  result["chip"] = ESP.getChipModel();
  result["free_heap"] = ESP.getFreeHeap();
  result["flash_size"] = ESP.getFlashChipSize();
  result["psram_found"] = psramFound();
  result["psram_size"] = ESP.getPsramSize();
  result["free_psram"] = ESP.getFreePsram();
  result["cc1101_found"] = radioFound;
  if (radioFound) {
    result["cc1101_partnum"] = radio.SpiReadStatus(REG_PARTNUM);
    result["cc1101_version"] = radio.SpiReadStatus(REG_VERSION);
  }
  result["cc1101_calibrated"] = radioCalibrated;
  result["cc1101_calibration_marcstate"] = radioCalibrationMarcState;
  result["cc1101_calibration_error"] = radioCalibrationError;
  JsonObject pins = result["pins"].to<JsonObject>();
  pins["sck"] = PIN_SCK;
  pins["mosi"] = PIN_MOSI;
  pins["miso"] = PIN_MISO;
  pins["csn"] = PIN_CSN;
  pins["gdo0"] = PIN_GDO0;
  pins["gdo2"] = PIN_GDO2;
  JsonObject wifi = result["wifi"].to<JsonObject>();
  addWifiStatus(wifi);
  JsonObject ble = result["ble"].to<JsonObject>();
  ble["enabled"] = BLE_ENABLED;
  ble["device_name"] = BLE_DEVICE_NAME;
  ble["service_uuid"] = BLE_SERVICE_UUID;
  ble["command_uuid"] = BLE_COMMAND_UUID;
  ble["response_uuid"] = BLE_RESPONSE_UUID;
  JsonObject receive = result["receiver"].to<JsonObject>();
  addReceiverConfig(receive);
  JsonObject recording = result["recording_config"].to<JsonObject>();
  addRecordingConfig(recording, recordingConfig);
  JsonObject storage = result["storage"].to<JsonObject>();
  addRecorderStatus(storage);
  respondOk(id, result);
}

void handleStatus(const String& id) {
  JsonDocument result;
  result["radio_found"] = radioFound;
  result["tx_busy"] = txBusy;
  result["abort_requested"] = abortRequested;
  result["cc1101_calibrated"] = radioCalibrated;
  result["cc1101_calibration_marcstate"] = radioCalibrationMarcState;
  result["cc1101_calibration_error"] = radioCalibrationError;
  result["frequency_mhz"] = static_cast<double>(receiverConfig.frequencyHz) / 1000000.0;
  if (!txBusy && radioFound) {
    result["rssi_dbm"] = radio.getRssi();
    result["lqi"] = radio.getLqi();
    result["marcstate"] = radio.SpiReadStatus(REG_MARCSTATE) & 0x1F;
    result["rx_bytes"] = radio.SpiReadStatus(REG_RXBYTES) & 0x7F;
  }
  JsonObject receive = result["receiver"].to<JsonObject>();
  addReceiverConfig(receive);
  JsonObject recording = result["recording_config"].to<JsonObject>();
  addRecordingConfig(recording, recordingConfig);
  JsonObject wifi = result["wifi"].to<JsonObject>();
  addWifiStatus(wifi);
  JsonObject storage = result["storage"].to<JsonObject>();
  addRecorderStatus(storage);
  respondOk(id, result);
}

void handleUsbPortStatus(const String& id) {
  JsonDocument result;
  usb_host_lib_info_t hostInfo;
  const esp_err_t infoError = usb_host_lib_info(&hostInfo);
  if (infoError == ESP_OK) {
    result["host_lib_ok"] = true;
    result["num_devices"] = hostInfo.num_devices;
    result["num_clients"] = hostInfo.num_clients;
  } else {
    result["host_lib_ok"] = false;
    result["host_lib_error"] = esp_err_to_name(infoError);
  }
  const uint32_t hprt = USB_DWC.hprt_reg.val;
  result["hprt"] = hprt;
  result["prtconnsts"] = static_cast<uint8_t>((hprt >> 0) & 1);
  result["prtconndet"] = static_cast<uint8_t>((hprt >> 1) & 1);
  result["prtena"] = static_cast<uint8_t>((hprt >> 2) & 1);
  result["prtovrcurract"] = static_cast<uint8_t>((hprt >> 4) & 1);
  result["prtlnsts"] = static_cast<uint8_t>((hprt >> 9) & 3);
  result["prtpwr"] = static_cast<uint8_t>((hprt >> 12) & 1);
  result["prtspd"] = static_cast<uint8_t>((hprt >> 17) & 3);
  const uint32_t gotgctl = USB_DWC.gotgctl_reg.val;
  result["gotgctl"] = gotgctl;
  result["curmod"] = static_cast<uint8_t>((gotgctl >> 21) & 1);
  result["conidsts"] = static_cast<uint8_t>((gotgctl >> 16) & 1);
  result["asesvld"] = static_cast<uint8_t>((gotgctl >> 18) & 1);
  result["bsesvld"] = static_cast<uint8_t>((gotgctl >> 19) & 1);
  respondOk(id, result);
}

void handleUsbSetPinsSwap(const String& id, JsonObject args) {
  const bool swap = args["swap"] | false;
  USB_WRAP.otg_conf.exchg_pins_override = 1;
  USB_WRAP.otg_conf.exchg_pins = swap ? 1 : 0;
  JsonDocument result;
  result["swap"] = swap;
  result["exchg_pins_override"] = static_cast<uint8_t>(USB_WRAP.otg_conf.exchg_pins_override);
  result["exchg_pins"] = static_cast<uint8_t>(USB_WRAP.otg_conf.exchg_pins);
  respondOk(id, result);
}

bool commandNeedsRadio(const String& command);

bool rejectWhileBusy(const String& id, const String& command) {
  if (!txBusy || !commandNeedsRadio(command) || command == "ABORT" || command == "GET_STATUS"
      || command == "GET_TX_RESULT"
      || command == "USB_PORT_STATUS" || command == "USB_SET_PINS_SWAP") return false;
  respondError(id, "radio is busy; use ABORT first");
  return true;
}

bool rejectWhileRecording(const String& id, const String& command) {
  if (!recorder.snapshot().recording) return false;
  if (command == "GET_INFO" || command == "GET_STATUS" || command == "GET_COMMANDS"
      || command == "GET_NETWORK_STATUS" || command == "GET_CAPTURE_CONFIG"
      || command == "GET_TX_RESULT"
      || command == "LIST_PROFILES" || command == "GET_PROFILE"
      || command == "GET_CLIENT_RECORDING_INFO"
      || command == "USB_PORT_STATUS" || command == "USB_SET_PINS_SWAP"
      || command == "STOP_RECORDING" || command == "STOP_CLIENT_RECORDING"
      || command == "READ_CLIENT_RECORDING"
      || command == "LIST_RECORDINGS" || command == "ABORT") {
    return false;
  }
  respondError(id, "recording is active; stop recording before changing radio state");
  return true;
}

bool commandNeedsRadio(const String& command) {
  return command != "GET_INFO" && command != "GET_STATUS" && command != "GET_COMMANDS"
    && command != "GET_NETWORK_STATUS" && command != "GET_CAPTURE_CONFIG"
    && command != "GET_TX_RESULT"
    && command != "SET_WIFI_CONFIG" && command != "CONNECT_WIFI"
    && command != "LIST_PROFILES" && command != "GET_PROFILE"
    && command != "SET_PROFILE" && command != "DELETE_PROFILE"
    && command != "GET_CLIENT_RECORDING_INFO"
    && command != "USB_PORT_STATUS" && command != "USB_SET_PINS_SWAP"
    && command != "LIST_RECORDINGS" && command != "DELETE_RECORDING"
    && command != "STOP_RECORDING"
    && command != "STOP_CLIENT_RECORDING" && command != "READ_CLIENT_RECORDING";
}

void handleCommand(const String& line) {
  JsonDocument request;
  const DeserializationError parseError = deserializeJson(request, line);
  if (parseError) {
    respondError("", String("invalid JSON: ") + parseError.c_str());
    return;
  }

  const String id = request["id"] | "";
  String command = request["cmd"] | "";
  command.toUpperCase();
  JsonObject args = request["args"].as<JsonObject>();
  if (id.isEmpty() || command.isEmpty()) {
    respondError(id, "id and cmd are required");
    return;
  }
  JsonDocument mergedArgs;
  if (commandSupportsSignalProfile(command)
      && (args["profile"].is<const char*>() || args["profile_name"].is<const char*>())) {
    JsonObject merged = mergedArgs.to<JsonObject>();
    String profileError;
    if (!mergeSignalProfileArgs(args, mergedArgs, merged, profileError)) {
      respondError(id, profileError);
      return;
    }
    args = merged;
  }
  if (!radioFound && commandNeedsRadio(command) && command != "CC_RESET") {
    respondError(id, "cc1101 not found");
    return;
  }
  if (rejectWhileBusy(id, command)) return;
  if (rejectWhileRecording(id, command)) return;

  if (command == "GET_INFO") {
    handleGetInfo(id);
  } else if (command == "GET_COMMANDS") {
    JsonDocument result;
    JsonArray commands = result["commands"].to<JsonArray>();
    for (const char* name : {
      "GET_INFO", "GET_STATUS", "GET_COMMANDS", "GET_NETWORK_STATUS",
      "GET_TX_RESULT",
      "GET_CAPTURE_CONFIG",
      "SET_RX_CONFIG", "SET_RECORDING_CONFIG", "SET_CAPTURE_CONFIG",
      "START_RECORDING", "STOP_RECORDING", "LIST_RECORDINGS", "DELETE_RECORDING",
      "START_CLIENT_RECORDING", "GET_CLIENT_RECORDING_INFO", "READ_CLIENT_RECORDING",
      "STOP_CLIENT_RECORDING",
      "USB_PORT_STATUS", "USB_SET_PINS_SWAP",
      "CC_RESET", "CC_SELF_TEST", "REG_READ", "REG_WRITE", "STROBE", "FIFO_READ", "FIFO_WRITE",
      "RX_PACKET", "RX_PULSES", "TX_PACKET", "TX_PULSES", "ABORT",
      "SET_WIFI_CONFIG", "CONNECT_WIFI", "LIST_PROFILES", "GET_PROFILE",
      "SET_PROFILE", "DELETE_PROFILE"
    }) commands.add(name);
    result["command_count"] = commands.size();
    result["api_version"] = FIRMWARE_VERSION;
    result["http_endpoint"] = "/api/command";
    result["transports"] = "UART and BLE use newline-delimited JSON; Wi-Fi HTTP uses the same JSON objects at /api/command";
    result["ble_enabled"] = BLE_ENABLED;
    JsonObject txLimits = result["tx_limits"].to<JsonObject>();
    txLimits["maximum_repeat"] = MAX_REPEAT;
    txLimits["frequency_bands_hz"] = "300000000..348000000,378000000..464000000,779000000..928000000";
    result["client_recording_transport"] = "Acknowledged chunks are available over UART, BLE, or Wi-Fi HTTP and contain up to 512 LSR1 records";
    JsonObject limits = result["recording_limits"].to<JsonObject>();
    limits["psram_buffer_bytes"] = 4 * 1024 * 1024;
    limits["maximum_client_chunk_records"] = 512;
    limits["maximum_duration_ms"] = 24UL * 60 * 60 * 1000;
    limits["maximum_min_edge_interval_us"] = 1000000;
    respondOk(id, result);
  } else if (command == "GET_TX_RESULT") {
    const String requestId = args["request_id"] | "";
    if (requestId.isEmpty()) {
      respondError(id, "request_id is required");
      return;
    }
    JsonDocument result;
    if (!addCachedTxResult(requestId, result)) {
      respondError(id, "TX result not found; request_id may be expired");
      return;
    }
    respondOk(id, result);
  } else if (command == "GET_NETWORK_STATUS") {
    JsonDocument result;
    JsonObject wifi = result["wifi"].to<JsonObject>();
    addWifiStatus(wifi);
    result["boot_id"] = bootId;
    respondOk(id, result);
  } else if (command == "SET_WIFI_CONFIG") {
    if (!args["ssid"].is<const char*>()) {
      respondError(id, "ssid is required");
      return;
    }
    const String requestedSsid = args["ssid"] | "";
    const String requestedPassword = args["password"].is<const char*>()
      ? String(args["password"] | "")
      : wifiPassword;
    if (requestedSsid.isEmpty() || requestedSsid.length() > 32) {
      respondError(id, "ssid must contain 1..32 characters");
      return;
    }
    if (requestedPassword.length() > 64) {
      respondError(id, "password must contain at most 64 characters");
      return;
    }
    wifiSsid = requestedSsid;
    wifiPassword = requestedPassword;
    persistWifiConfig();
    requestWifiConnection();
    const bool connected = WiFi.status() == WL_CONNECTED;
    JsonDocument result;
    result["status"] = connected ? "connected" : "connecting";
    result["persistent"] = true;
    JsonObject wifi = result["wifi"].to<JsonObject>();
    addWifiStatus(wifi);
    result["connected"] = connected;
    result["ip"] = connected ? WiFi.localIP().toString() : "0.0.0.0";
    respondOk(id, result);
  } else if (command == "CONNECT_WIFI") {
    requestWifiConnection();
    const bool connected = WiFi.status() == WL_CONNECTED;
    JsonDocument result;
    result["status"] = connected ? "connected" : "connecting";
    JsonObject wifi = result["wifi"].to<JsonObject>();
    addWifiStatus(wifi);
    result["connected"] = connected;
    result["ip"] = connected ? WiFi.localIP().toString() : "0.0.0.0";
    respondOk(id, result);
  } else if (command == "LIST_PROFILES") {
    JsonDocument result;
    result["count"] = signalProfiles.size();
    JsonArray profiles = result["profiles"].to<JsonArray>();
    for (const SignalProfile& profile : signalProfiles) {
      JsonObject item = profiles.add<JsonObject>();
      item["name"] = profile.name;
      item["kind"] = profile.kind;
      item["frequency_hz"] = profile.frequencyHz;
      item["pulse_count"] = profile.durationsUs.size();
      item["data_bytes"] = profile.data.size();
    }
    respondOk(id, result);
  } else if (command == "GET_PROFILE") {
    const String name = args["name"].is<const char*>()
      ? String(args["name"] | "")
      : String(args["profile"] | "");
    const SignalProfile* profile = findSignalProfile(name);
    if (profile == nullptr) {
      respondError(id, "signal profile not found");
      return;
    }
    JsonDocument result;
    addSignalProfile(result.to<JsonObject>(), *profile);
    respondOk(id, result);
  } else if (command == "SET_PROFILE") {
    JsonObjectConst profileArgs = args["profile"].is<JsonObject>()
      ? args["profile"].as<JsonObjectConst>()
      : args;
    const String name = profileArgs["name"] | "";
    const int index = findSignalProfileIndex(name);
    const SignalProfile* base = index < 0 ? nullptr : &signalProfiles[static_cast<size_t>(index)];
    SignalProfile profile;
    String error;
    if (!parseSignalProfile(profileArgs, base, profile, error)) {
      respondError(id, error);
      return;
    }
    if (!saveSignalProfile(profile, error)) {
      respondError(id, error);
      return;
    }
    JsonDocument result;
    result["status"] = "saved";
    addSignalProfile(result["profile"].to<JsonObject>(), profile);
    respondOk(id, result);
  } else if (command == "DELETE_PROFILE") {
    const String name = args["name"].is<const char*>()
      ? String(args["name"] | "")
      : String(args["profile"] | "");
    String error;
    if (!deleteSignalProfile(name, error)) {
      respondError(id, error);
      return;
    }
    respondSimple(id, "deleted");
  } else if (command == "GET_CAPTURE_CONFIG") {
    JsonDocument result;
    JsonObject receive = result["receiver"].to<JsonObject>();
    addReceiverConfig(receive, receiverConfig);
    JsonObject recording = result["recording"].to<JsonObject>();
    addRecordingConfig(recording, recordingConfig);
    result["persistent"] = true;
    result["recording_active"] = recorder.snapshot().recording;
    respondOk(id, result);
  } else if (command == "USB_PORT_STATUS") {
    handleUsbPortStatus(id);
  } else if (command == "USB_SET_PINS_SWAP") {
    handleUsbSetPinsSwap(id, args);
  } else if (command == "SET_RX_CONFIG" || command == "SET_CAPTURE_CONFIG") {
    ReceiverConfig requestedReceiver;
    String configError;
    JsonObjectConst receiverArgs = args["receiver"].is<JsonObject>()
      ? args["receiver"].as<JsonObjectConst>()
      : args;
    if (!parseReceiverConfig(receiverArgs, receiverConfig, requestedReceiver, configError)) {
      respondError(id, configError);
      return;
    }
    const ReceiverConfig previousConfig = receiverConfig;
    receiverConfig = requestedReceiver;
    radioCalibrated = false;
    String calibrationError;
    if (!prepareAsyncReceiver(calibrationError)) {
      receiverConfig = previousConfig;
      String restoreError;
      prepareAsyncReceiver(restoreError);
      idleRadio();
      respondError(id, calibrationError);
      return;
    }
    persistReceiverConfig();
    if (command == "SET_CAPTURE_CONFIG") {
      RecordingConfig requestedRecording;
      JsonObjectConst recordingArgs = args["recording"].is<JsonObject>()
        ? args["recording"].as<JsonObjectConst>()
        : JsonObjectConst();
      if (!parseRecordingConfig(recordingArgs, recordingConfig, requestedRecording, configError)) {
        receiverConfig = previousConfig;
        persistReceiverConfig();
        prepareAsyncReceiver(receiverConfig, calibrationError);
        idleRadio();
        respondError(id, configError);
        return;
      }
      recordingConfig = requestedRecording;
      persistRecordingConfig();
    }
    idleRadio();
    JsonDocument result;
    JsonObject config = result["receiver"].to<JsonObject>();
    addReceiverConfig(config);
    JsonObject capture = result["recording"].to<JsonObject>();
    addRecordingConfig(capture, recordingConfig);
    result["status"] = "configured";
    respondOk(id, result);
  } else if (command == "SET_RECORDING_CONFIG") {
    RecordingConfig requested;
    String error;
    const JsonObjectConst recordingArgs = args["recording"].is<JsonObject>()
      ? args["recording"].as<JsonObjectConst>()
      : args;
    if (!parseRecordingConfig(recordingArgs, recordingConfig, requested, error)) {
      respondError(id, error);
      return;
    }
    recordingConfig = requested;
    persistRecordingConfig();
    JsonDocument result;
    result["status"] = "configured";
    JsonObject config = result["recording"].to<JsonObject>();
    addRecordingConfig(config, recordingConfig);
    respondOk(id, result);
  } else if (command == "START_RECORDING") {
    ReceiverConfig sessionReceiver;
    RecordingConfig sessionRecording;
    String error;
    const JsonObjectConst receiverArgs = args["receiver"].is<JsonObject>()
      ? args["receiver"].as<JsonObjectConst>()
      : JsonObjectConst();
    const JsonObjectConst recordingArgs = args["recording"].is<JsonObject>()
      ? args["recording"].as<JsonObjectConst>()
      : JsonObjectConst();
    if (!parseReceiverConfig(receiverArgs, receiverConfig, sessionReceiver, error)
        || !parseRecordingConfig(recordingArgs, recordingConfig, sessionRecording, error)) {
      respondError(id, error);
      return;
    }
    if (!prepareAsyncReceiver(sessionReceiver, error)) {
      respondError(id, error);
      return;
    }
    const String name = args["name"] | "capture";
    const uint64_t utcEpochMs = args["utc_epoch_ms"].as<uint64_t>();
    if (!recorder.start(name, sessionReceiver, sessionRecording, utcEpochMs, error)) {
      idleRadio();
      respondError(id, error);
      return;
    }
    radio.SetRx();
    uint8_t rxState = 0;
    if (!waitForMarcState(MARCSTATE_RX, 50, rxState)) {
      String stopError;
      recorder.stop(stopError, "radio_start_failed");
      idleRadio();
      respondError(id, String("cc1101 failed to enter RX; MARCSTATE=") + rxState);
      return;
    }
    JsonDocument result;
    result["status"] = "recording";
    result["target"] = "usb";
    JsonObject receive = result["receiver"].to<JsonObject>();
    addReceiverConfig(receive, sessionReceiver);
    JsonObject recording = result["recording"].to<JsonObject>();
    addRecordingConfig(recording, sessionRecording);
    JsonObject storage = result["storage"].to<JsonObject>();
    addRecorderStatus(storage);
    respondOk(id, result);
  } else if (command == "STOP_RECORDING") {
    if (recorder.snapshot().recordingTarget == "client") {
      respondError(id, "client recording requires STOP_CLIENT_RECORDING with its session_id");
      return;
    }
    String error;
    const bool stopped = recorder.stop(error);
    if (radioFound && !txBusy) idleRadio();
    if (!stopped) {
      respondError(id, error);
      return;
    }
    JsonDocument result;
    result["status"] = "stopped";
    JsonObject storage = result["storage"].to<JsonObject>();
    addRecorderStatus(storage);
    respondOk(id, result);
  } else if (command == "START_CLIENT_RECORDING") {
    ReceiverConfig sessionReceiver;
    RecordingConfig sessionRecording;
    String error;
    const JsonObjectConst receiverArgs = args["receiver"].is<JsonObject>()
      ? args["receiver"].as<JsonObjectConst>()
      : JsonObjectConst();
    const JsonObjectConst recordingArgs = args["recording"].is<JsonObject>()
      ? args["recording"].as<JsonObjectConst>()
      : JsonObjectConst();
    if (!parseReceiverConfig(receiverArgs, receiverConfig, sessionReceiver, error)
        || !parseRecordingConfig(recordingArgs, recordingConfig, sessionRecording, error)) {
      respondError(id, error);
      return;
    }
    if (!prepareAsyncReceiver(sessionReceiver, error)) {
      respondError(id, error);
      return;
    }
    ClientRecordingStart start;
    const String name = args["name"] | "capture";
    const uint64_t utcEpochMs = args["utc_epoch_ms"].as<uint64_t>();
    if (!recorder.startClient(name, sessionReceiver, sessionRecording, utcEpochMs, start, error)) {
      idleRadio();
      respondError(id, error);
      return;
    }
    radio.SetRx();
    uint8_t rxState = 0;
    if (!waitForMarcState(MARCSTATE_RX, 50, rxState)) {
      String stopError;
      recorder.stop(stopError, "radio_start_failed");
      idleRadio();
      respondError(id, String("cc1101 failed to enter RX; MARCSTATE=") + rxState);
      return;
    }
    JsonDocument result;
    result["status"] = "recording";
    result["target"] = "client";
    result["session_id"] = start.sessionId;
    result["recording_name"] = start.recordingName;
    result["format"] = "LSR1";
    result["format_version"] = 1;
    result["header_size_bytes"] = 40;
    result["record_size_bytes"] = 16;
    result["header_base64"] = start.headerBase64;
    JsonObject receive = result["receiver"].to<JsonObject>();
    addReceiverConfig(receive, sessionReceiver);
    JsonObject recording = result["recording"].to<JsonObject>();
    addRecordingConfig(recording, sessionRecording);
    JsonObject storage = result["storage"].to<JsonObject>();
    addRecorderStatus(storage);
    respondOk(id, result);
  } else if (command == "GET_CLIENT_RECORDING_INFO") {
    const RecorderSnapshot snapshot = recorder.snapshot();
    const String sessionId = args["session_id"] | "";
    if (!snapshot.clientRecordingAvailable) {
      respondError(id, "no client recording is available");
      return;
    }
    if (sessionId.isEmpty() || sessionId != snapshot.clientSessionId) {
      respondError(id, "client recording session_id does not match");
      return;
    }
    JsonDocument result;
    result["session_id"] = snapshot.clientSessionId;
    result["recording_name"] = snapshot.recordingName;
    result["format"] = "LSR1";
    result["format_version"] = 1;
    result["header_size_bytes"] = 40;
    result["record_size_bytes"] = 16;
    result["header_base64"] = snapshot.clientHeaderBase64;
    result["active"] = snapshot.recording;
    result["end_of_stream"] = snapshot.clientEndOfStream;
    result["acknowledged_records"] = snapshot.clientAcknowledgedRecords;
    result["pending_records"] = snapshot.clientPendingRecords;
    result["buffered_records"] = snapshot.clientBufferedRecords;
    result["captured_edges"] = snapshot.capturedEdges;
    result["written_edges"] = snapshot.writtenEdges;
    result["filtered_edges"] = snapshot.filteredEdges;
    result["dropped_isr_edges"] = snapshot.droppedIsrEdges;
    result["dropped_psram_edges"] = snapshot.droppedPsramEdges;
    result["stop_reason"] = snapshot.stopReason;
    result["complete_without_known_drops"] = snapshot.clientEndOfStream
      && snapshot.droppedIsrEdges == 0 && snapshot.droppedPsramEdges == 0;
    JsonObject receive = result["receiver"].to<JsonObject>();
    addReceiverConfig(receive, snapshot.activeReceiverConfig);
    JsonObject recording = result["recording"].to<JsonObject>();
    addRecordingConfig(recording, snapshot.activeRecordingConfig);
    respondOk(id, result);
  } else if (command == "READ_CLIENT_RECORDING") {
    const RecorderSnapshot snapshot = recorder.snapshot();
    const int maximum = args["max_records"].is<int>()
      ? args["max_records"].as<int>()
      : snapshot.activeRecordingConfig.clientChunkRecords;
    if (maximum < 1 || maximum > 512) {
      respondError(id, "max_records must be in 1..512");
      return;
    }
    ClientRecordingChunk chunk;
    String error;
    if (!recorder.readClient(
          String(args["session_id"] | ""),
          args["ack_sequence"].as<uint64_t>(),
          static_cast<size_t>(maximum),
          chunk,
          error
        )) {
      respondError(id, error);
      return;
    }
    JsonDocument result;
    result["session_id"] = chunk.sessionId;
    result["recording_name"] = chunk.recordingName;
    result["sequence_start"] = chunk.sequenceStart;
    result["sequence_end"] = chunk.sequenceEnd;
    result["offset_bytes"] = chunk.offsetBytes;
    result["next_offset_bytes"] = chunk.nextOffsetBytes;
    result["record_count"] = chunk.recordCount;
    result["record_size_bytes"] = 16;
    result["data_crc32"] = chunk.dataCrc32;
    result["data_base64"] = chunk.dataBase64;
    result["active"] = chunk.active;
    result["end_of_stream"] = chunk.endOfStream;
    JsonObject storage = result["storage"].to<JsonObject>();
    addRecorderStatus(storage);
    respondOk(id, result);
  } else if (command == "STOP_CLIENT_RECORDING") {
    String error;
    if (!recorder.stopClient(String(args["session_id"] | ""), error)) {
      respondError(id, error);
      return;
    }
    if (radioFound && !txBusy) idleRadio();
    JsonDocument result;
    result["status"] = "stopped";
    result["target"] = "client";
    JsonObject storage = result["storage"].to<JsonObject>();
    addRecorderStatus(storage);
    respondOk(id, result);
  } else if (command == "LIST_RECORDINGS") {
    String error;
    const std::vector<RecordingFileInfo> files = recorder.listFiles(error);
    if (!error.isEmpty()) {
      respondError(id, error);
      return;
    }
    JsonDocument result;
    JsonArray output = result["files"].to<JsonArray>();
    for (const RecordingFileInfo& file : files) {
      JsonObject item = output.add<JsonObject>();
      item["name"] = file.name;
      item["size_bytes"] = file.sizeBytes;
    }
    respondOk(id, result);
  } else if (command == "DELETE_RECORDING") {
    String error;
    if (!recorder.deleteFile(String(args["name"] | ""), error)) {
      respondError(id, error);
      return;
    }
    respondSimple(id, "deleted");
  } else if (command == "CC_RESET" || command == "CC_SELF_TEST") {
    String error;
    const bool prepared = resetAndPrepareRadio(error);
    JsonDocument result;
    result["status"] = command == "CC_RESET" ? "reset" : "tested";
    result["passed"] = prepared;
    result["cc1101_found"] = radioFound;
    if (radioFound) {
      const uint8_t partnum = radio.SpiReadStatus(REG_PARTNUM);
      const uint8_t version = radio.SpiReadStatus(REG_VERSION);
      result["cc1101_partnum"] = partnum;
      result["cc1101_version"] = version;
      result["partnum_expected"] = 0;
      result["partnum_matches"] = partnum == 0;
      result["passed"] = prepared && partnum == 0;
    }
    result["calibrated"] = radioCalibrated;
    result["marcstate"] = radioCalibrationMarcState;
    if (!error.isEmpty()) result["error"] = error;
    respondOk(id, result);
  } else if (command == "REG_READ") {
    const uint8_t address = args["address"] | 0;
    JsonDocument result;
    result["address"] = address;
    result["value"] = address >= 0x30 ? radio.SpiReadStatus(address) : radio.SpiReadReg(address);
    respondOk(id, result);
  } else if (command == "REG_WRITE") {
    const uint8_t address = args["address"] | 0;
    const uint8_t value = args["value"] | 0;
    radio.SpiWriteReg(address, value);
    JsonDocument result;
    result["address"] = address;
    result["value"] = radio.SpiReadReg(address);
    respondOk(id, result);
  } else if (command == "STROBE") {
    const uint8_t value = args["value"] | 0;
    radio.SpiStrobe(value);
    JsonDocument result;
    result["strobe"] = value;
    result["marcstate"] = radio.SpiReadStatus(REG_MARCSTATE) & 0x1F;
    respondOk(id, result);
  } else if (command == "FIFO_WRITE") {
    std::vector<uint8_t> data;
    String error;
    if (!parseHex(String(args["data_hex"] | ""), data, error) || data.empty() || data.size() > 64) {
      respondError(id, error.isEmpty() ? "FIFO_WRITE requires 1..64 bytes" : error);
      return;
    }
    radio.SpiWriteBurstReg(FIFO_ADDRESS, data.data(), data.size());
    JsonDocument result;
    result["bytes_written"] = data.size();
    respondOk(id, result);
  } else if (command == "FIFO_READ") {
    const size_t length = constrain(static_cast<int>(args["length"] | 1), 1, 64);
    std::vector<uint8_t> data(length);
    radio.SpiReadBurstReg(FIFO_ADDRESS, data.data(), data.size());
    JsonDocument result;
    result["data_hex"] = bytesToHex(data.data(), data.size());
    respondOk(id, result);
  } else if (command == "TX_PACKET") {
    std::vector<uint8_t> data;
    String error;
    if (!parseHex(String(args["data_hex"] | ""), data, error) || data.empty() || data.size() > 61) {
      respondError(id, error.isEmpty() ? "TX_PACKET requires 1..61 bytes" : error);
      return;
    }
    const int requestedRepeat = args["repeat"] | 1;
    if (requestedRepeat < 1 || requestedRepeat > MAX_REPEAT) {
      respondError(id, "repeat must be in 1..10");
      return;
    }
    const uint8_t repeat = static_cast<uint8_t>(requestedRepeat);
    const uint32_t gapUs = constrain(static_cast<uint32_t>(args["gap_us"] | 20000), 1000UL, 1000000UL);
    const uint32_t frequencyHz = args["frequency_hz"] | 433920000UL;
    if (!validCc1101Frequency(frequencyHz)) {
      respondError(id, "frequency_hz must be in a CC1101 band: 300-348, 378-464, or 779-928 MHz");
      return;
    }
    const int8_t powerDbm = constrain(static_cast<int>(args["power_dbm"] | -30), -30, 10);
    configurePacketMode(frequencyHz, powerDbm);
    uint8_t txState = 0;
    uint8_t sequencesSent = 0;
    String txError;
    for (uint8_t attempt = 0; attempt < repeat; ++attempt) {
      if (!sendPacketOnce(data, txState, txError)) break;
      ++sequencesSent;
      if (attempt + 1 < repeat) delayMicroseconds(gapUs);
    }
    idleRadio();
    if (!txError.isEmpty()) {
      respondError(id, txError);
      return;
    }
    JsonDocument result;
    result["status"] = "sent";
    result["bytes"] = data.size();
    result["repeat"] = repeat;
    result["sequences_sent"] = sequencesSent;
    result["tx_started"] = sequencesSent > 0;
    result["tx_completed"] = sequencesSent == repeat;
    result["tx_state"] = txState;
    respondOk(id, result);
  } else if (command == "RX_PACKET") {
    const uint32_t timeoutMs = constrain(static_cast<uint32_t>(args["timeout_ms"] | 1000), 10UL, 5000UL);
    const size_t maxLength = constrain(static_cast<int>(args["max_length"] | 64), 1, 64);
    const JsonObjectConst receiverArgs = args["receiver"].is<JsonObject>()
      ? args["receiver"].as<JsonObjectConst>()
      : args;
    ReceiverConfig packetReceiver;
    String receiverError;
    if (!parseReceiverConfig(receiverArgs, receiverConfig, packetReceiver, receiverError)) {
      respondError(id, receiverError);
      return;
    }
    const uint32_t frequencyHz = packetReceiver.frequencyHz;
    configurePacketMode(frequencyHz, -30);
    String calibrationError;
    if (!calibrateRadio(calibrationError)) {
      idleRadio();
      respondError(id, calibrationError);
      return;
    }
    radio.SetRx();
    const uint32_t start = millis();
    while (!radio.CheckReceiveFlag() && millis() - start < timeoutMs) delay(1);
    std::vector<uint8_t> data(maxLength);
    uint8_t length = 0;
    if (radio.CheckReceiveFlag()) length = min(static_cast<size_t>(radio.ReceiveData(data.data())), maxLength);
    idleRadio();
    JsonDocument result;
    result["received"] = length > 0;
    result["data_hex"] = bytesToHex(data.data(), length);
    respondOk(id, result);
  } else if (command == "TX_PULSES") {
    JsonArray durations = args["durations_us"].as<JsonArray>();
    if (durations.isNull() || durations.size() == 0 || durations.size() > MAX_PULSES) {
      respondError(id, "durations_us requires 1..2048 entries");
      return;
    }
    auto* config = new PulseTaskConfig();
    config->requestId = id;
    config->frequencyHz = args["frequency_hz"] | 433920000UL;
    if (!validCc1101Frequency(config->frequencyHz)) {
      delete config;
      respondError(id, "frequency_hz must be in a CC1101 band: 300-348, 378-464, or 779-928 MHz");
      return;
    }
    config->powerDbm = constrain(static_cast<int>(args["power_dbm"] | -30), -30, 10);
    const int requestedRepeat = args["repeat"] | 1;
    if (requestedRepeat < 1 || requestedRepeat > MAX_REPEAT) {
      delete config;
      respondError(id, "repeat must be in 1..10");
      return;
    }
    config->repeat = static_cast<uint8_t>(requestedRepeat);
    config->gapUs = constrain(static_cast<uint32_t>(args["gap_us"] | 20000), 1000UL, 1000000UL);
    config->startLevel = (args["start_level"] | 0) ? 1 : 0;
    config->durations.reserve(durations.size());
    uint64_t totalUs = 0;
    for (JsonVariant value : durations) {
      const uint32_t duration = constrain(value.as<uint32_t>(), 10UL, MAX_PULSE_US);
      totalUs += duration;
      config->durations.push_back(duration);
    }
    if (totalUs > MAX_SEQUENCE_US) {
      delete config;
      respondError(id, "one pulse sequence cannot exceed 1000 ms");
      return;
    }
    cacheTxPending(config->requestId, "TX_PULSES");
    abortRequested = false;
    txBusy = true;
    if (pulseQueue == nullptr || xQueueSend(pulseQueue, &config, 0) != pdTRUE) {
      txBusy = false;
      cacheTxError(config->requestId, "failed to queue TX task", config->frequencyHz);
      delete config;
      respondError(id, "failed to queue TX task");
    } else {
      JsonDocument result;
      result["status"] = "pending";
      result["accepted"] = true;
      result["request_id"] = config->requestId;
      result["command"] = "TX_PULSES";
      respondOk(id, result);
    }
  } else if (command == "TX_BYTES") {
    // 通用紧凑发射: 主机只发帧字节的 hex, 固件负责前导/0,1,d 展开与游程编码。
    // 相比 TX_PULSES 的 durations_us 载荷, 7 字节帧的命令从 ~1.2KB 降到 ~150B。
    const String dataHex = args["data_hex"] | "";
    if (dataHex.isEmpty() || (dataHex.length() % 2) != 0
        || dataHex.length() / 2 > MAX_AIR_FRAME_BYTES) {
      respondError(id, "data_hex requires an even-length hex string of 1..64 bytes");
      return;
    }
    const size_t frameLength = dataHex.length() / 2;
    uint8_t frameBytes[MAX_AIR_FRAME_BYTES];
    for (size_t i = 0; i < frameLength; ++i) {
      const int8_t high = hexNibble(dataHex[i * 2]);
      const int8_t low = hexNibble(dataHex[i * 2 + 1]);
      if (high < 0 || low < 0) {
        respondError(id, "data_hex contains a non-hex character");
        return;
      }
      frameBytes[i] = static_cast<uint8_t>((high << 4) | low);
    }
    const uint32_t bitUs = constrain(static_cast<uint32_t>(args["bit_us"] | 250), 20UL, 2000UL);
    const int requestedRepeat = args["repeat"] | 1;
    if (requestedRepeat < 1 || requestedRepeat > MAX_REPEAT) {
      respondError(id, "repeat must be in 1..10");
      return;
    }

    auto* config = new PulseTaskConfig();
    config->commandLabel = "TX_BYTES";
    config->requestId = id;
    config->frequencyHz = args["frequency_hz"] | 433920000UL;
    if (!validCc1101Frequency(config->frequencyHz)) {
      delete config;
      respondError(id, "frequency_hz must be in a CC1101 band: 300-348, 378-464, or 779-928 MHz");
      return;
    }
    config->powerDbm = constrain(static_cast<int>(args["power_dbm"] | -30), -30, 10);
    config->repeat = static_cast<uint8_t>(requestedRepeat);
    config->gapUs = constrain(static_cast<uint32_t>(args["gap_us"] | 20000), 50UL, 1000000UL);
    config->startLevel = 1;
    config->durations.reserve(frameLength * 26);
    appendFrameDurations(frameBytes, frameLength, bitUs, config->durations);
    if (config->durations.size() > MAX_PULSES) {
      delete config;
      respondError(id, "TX_BYTES produced too many pulses; use fewer bytes");
      return;
    }
    uint64_t totalUs = 0;
    for (uint32_t duration : config->durations) totalUs += duration;
    if (totalUs * config->repeat > MAX_SEQUENCE_US) {
      delete config;
      respondError(id, "TX_BYTES sequence cannot exceed 1000 ms; reduce bytes/repeat/bit_us");
      return;
    }
    cacheTxPending(config->requestId, "TX_BYTES");
    abortRequested = false;
    txBusy = true;
    if (pulseQueue == nullptr || xQueueSend(pulseQueue, &config, 0) != pdTRUE) {
      txBusy = false;
      cacheTxError(config->requestId, "failed to queue TX task", config->frequencyHz);
      delete config;
      respondError(id, "failed to queue TX task");
    } else {
      JsonDocument result;
      result["status"] = "pending";
      result["accepted"] = true;
      result["request_id"] = config->requestId;
      result["command"] = "TX_BYTES";
      result["bytes"] = static_cast<uint32_t>(frameLength);
      result["bit_us"] = bitUs;
      result["repeat"] = requestedRepeat;
      result["frame_us"] = static_cast<uint32_t>(totalUs);
      result["duration_us"] = static_cast<uint32_t>(totalUs * config->repeat);
      respondOk(id, result);
    }
  } else if (command == "TX_D8") {
    JsonArray slotArray = args["slots"].as<JsonArray>();
    if (slotArray.isNull() || slotArray.size() != D8_SLOT_COUNT) {
      respondError(id, "slots requires exactly 9 16-bit words");
      return;
    }
    uint16_t slotWords[D8_SLOT_COUNT];
    for (uint16_t i = 0; i < D8_SLOT_COUNT; ++i) {
      slotWords[i] = static_cast<uint16_t>(slotArray[i].as<uint32_t>() & 0xFFFF);
    }
    int burst = args["burst"] | 6;
    if (burst != 1 && burst != 2 && burst != 3 && burst != 6) burst = 6;
    const uint32_t bitUs = constrain(static_cast<uint32_t>(args["bit_us"] | 250), 20UL, 2000UL);
    const uint32_t gapUs = constrain(static_cast<uint32_t>(args["gap_us"] | 850), 50UL, 100000UL);

    auto* config = new PulseTaskConfig();
    config->requestId = id;
    config->frequencyHz = args["frequency_hz"] | 433920000UL;
    if (!validCc1101Frequency(config->frequencyHz)) {
      delete config;
      respondError(id, "frequency_hz must be in a CC1101 band: 300-348, 378-464, or 779-928 MHz");
      return;
    }
    config->commandLabel = "TX_D8";
    config->powerDbm = constrain(static_cast<int>(args["power_dbm"] | -30), -30, 10);
    config->repeat = 1;
    config->gapUs = 20000;
    config->startLevel = 1;
    config->durations.reserve(static_cast<size_t>(burst) * 400);
    for (int frameIndex = 0; frameIndex < burst; ++frameIndex) {
      uint8_t frameBytes[D8_FRAME_BYTES];
      buildD8Frame(slotWords, D8_PHASE_SEQUENCE[frameIndex % 6], frameBytes);
      if (frameIndex > 0) {
        // 帧间保持低电平: 上一帧若结束于高电平则补一段低电平, 否则延长最后一段低电平。
        if (config->durations.size() % 2 == 1) {
          config->durations.push_back(gapUs);
        } else {
          config->durations.back() += gapUs;
        }
      }
      appendD8Durations(frameBytes, bitUs, config->durations);
    }
    if (config->durations.size() > MAX_PULSES) {
      delete config;
      respondError(id, "TX_D8 produced too many pulses; reduce burst");
      return;
    }
    uint64_t totalUs = 0;
    for (uint32_t duration : config->durations) totalUs += duration;
    if (totalUs > MAX_SEQUENCE_US) {
      delete config;
      respondError(id, "TX_D8 sequence cannot exceed 1000 ms; reduce burst or bit_us");
      return;
    }
    cacheTxPending(config->requestId, "TX_D8");
    abortRequested = false;
    txBusy = true;
    if (pulseQueue == nullptr || xQueueSend(pulseQueue, &config, 0) != pdTRUE) {
      txBusy = false;
      cacheTxError(config->requestId, "failed to queue TX task", config->frequencyHz);
      delete config;
      respondError(id, "failed to queue TX task");
    } else {
      JsonDocument result;
      result["status"] = "pending";
      result["accepted"] = true;
      result["request_id"] = config->requestId;
      result["command"] = "TX_D8";
      result["burst"] = burst;
      result["bit_us"] = bitUs;
      result["duration_us"] = static_cast<uint32_t>(totalUs);
      respondOk(id, result);
    }
  } else if (command == "RX_PULSES") {
    const uint32_t timeoutMs = constrain(static_cast<uint32_t>(args["timeout_ms"] | 1000), 10UL, 5000UL);
    const size_t maxEdges = constrain(static_cast<int>(args["max_edges"] | 512), 1, 2048);
    const JsonObjectConst receiverArgs = args["receiver"].is<JsonObject>()
      ? args["receiver"].as<JsonObjectConst>()
      : args;
    ReceiverConfig pulseReceiver;
    String calibrationError;
    if (!parseReceiverConfig(receiverArgs, receiverConfig, pulseReceiver, calibrationError)
        || !prepareAsyncReceiver(pulseReceiver, calibrationError)) {
      idleRadio();
      respondError(id, calibrationError);
      return;
    }
    radio.SetRx();
    std::vector<uint32_t> edgeDurations;
    edgeDurations.reserve(maxEdges);
    uint8_t startLevel = digitalRead(PIN_GDO0) ? 1 : 0;
    uint8_t level = startLevel;
    uint32_t lastEdge = micros();
    const uint32_t started = millis();
    while (millis() - started < timeoutMs && edgeDurations.size() < maxEdges) {
      const uint8_t next = digitalRead(PIN_GDO0) ? 1 : 0;
      if (next != level) {
        edgeDurations.push_back(micros() - lastEdge);
        lastEdge = micros();
        level = next;
      }
      yield();
    }
    idleRadio();
    JsonDocument result;
    result["start_level"] = startLevel;
    JsonObject receive = result["receiver"].to<JsonObject>();
    addReceiverConfig(receive, pulseReceiver);
    JsonArray output = result["durations_us"].to<JsonArray>();
    for (uint32_t duration : edgeDurations) output.add(duration);
    respondOk(id, result);
  } else if (command == "GET_STATUS") {
    handleStatus(id);
  } else if (command == "ABORT") {
    abortRequested = true;
    String pulseStopError;
    if (txBusy) stopPulseRmt(pulseStopError);
    const RecorderSnapshot before = recorder.snapshot();
    const bool wasRecording = before.recording;
    String stopError;
    if (wasRecording) {
      recorder.stop(stopError);
    }
    if (!txBusy && radioFound) idleRadio();
    const RecorderSnapshot after = recorder.snapshot();
    JsonDocument result;
    result["status"] = txBusy ? "abort_requested" : wasRecording ? "recording_stopped" : "idle";
    result["recording_target"] = before.recordingTarget;
    if (!pulseStopError.isEmpty()) result["pulse_stop_error"] = pulseStopError;
    if (!stopError.isEmpty()) result["recording_stop_error"] = stopError;
    if (before.recordingTarget == "client") {
      result["client_tail_preserved"] = true;
      result["session_id"] = after.clientSessionId;
    }
    JsonObject storage = result["storage"].to<JsonObject>();
    addRecorderStatus(storage);
    respondOk(id, result);
  } else {
    respondError(id, String("unknown command: ") + command);
  }
}

String runCommandForReply(const String& line) {
  String reply;
  if (commandMutex == nullptr || xSemaphoreTake(commandMutex, pdMS_TO_TICKS(10000)) != pdTRUE) {
    JsonDocument response;
    response["id"] = "";
    response["ok"] = false;
    response["error"] = "command processor is busy";
    serializeJson(response, reply);
    return reply;
  }
  responseSink = &reply;
  handleCommand(line);
  responseSink = nullptr;
  xSemaphoreGive(commandMutex);
  if (reply.isEmpty()) {
    JsonDocument response;
    response["id"] = "";
    response["ok"] = false;
    response["error"] = "command produced no response";
    serializeJson(response, reply);
  }
  return reply;
}

void runSerialCommand(const String& line) {
  if (commandMutex == nullptr || xSemaphoreTake(commandMutex, pdMS_TO_TICKS(10000)) != pdTRUE) {
    respondError("", "command processor is busy");
    return;
  }
  responseSink = nullptr;
  handleCommand(line);
  xSemaphoreGive(commandMutex);
}

void sendBleResponse(const String& response) {
  if (!bleConnected || bleResponseCharacteristic == nullptr) return;
  String framed = response;
  framed += '\n';
  for (size_t offset = 0; offset < framed.length(); offset += BLE_NOTIFY_CHUNK) {
    const size_t length = min(BLE_NOTIFY_CHUNK, framed.length() - offset);
    bleResponseCharacteristic->setValue(
      reinterpret_cast<uint8_t*>(const_cast<char*>(framed.c_str() + offset)),
      length
    );
    bleResponseCharacteristic->notify();
    delay(12);
  }
}

// 异步结果 (TX_COMPLETE) 回给最近发过 UDP 命令的那一端。UDP 是无连接的,
// 只能记最近一次来源; 同时用多个客户端时才需要改成分连接管理。
void sendUdpResponse(const String& response) {
  if (!udpPeerActive || !discoveryActive || udpPeerPort == 0) return;
  String framed = response;
  framed += (char)10;
  discoveryUdp.beginPacket(udpPeerIp, udpPeerPort);
  discoveryUdp.write(reinterpret_cast<const uint8_t*>(framed.c_str()), framed.length());
  discoveryUdp.endPacket();
}

class LightstickBleServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    bleConnected = true;
  }

  void onDisconnect(BLEServer*) override {
    bleConnected = false;
    BLEDevice::startAdvertising();
  }
};

class LightstickBleCommandCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    const std::string value = characteristic->getValue();
    for (const char character : value) {
      if (character == '\n') {
        bleInput.trim();
        if (!bleInput.isEmpty() && bleCommandQueue != nullptr) {
          String* command = new String(bleInput);
          if (xQueueSend(bleCommandQueue, &command, 0) != pdTRUE) delete command;
        }
        bleInput = "";
      } else if (character != '\r') {
        if (bleInput.length() < MAX_SERIAL_LINE) {
          bleInput += character;
        } else {
          bleInput = "";
        }
      }
    }
  }
};

void addCorsHeaders() {
  webServer.sendHeader("Access-Control-Allow-Origin", "*");
  webServer.sendHeader("Access-Control-Allow-Headers", "Content-Type");
  webServer.sendHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  webServer.sendHeader("Cache-Control", "no-store");
}

String makeSimpleRequest(const char* command) {
  JsonDocument request;
  request["id"] = String("http-") + millis();
  request["cmd"] = command;
  String json;
  serializeJson(request, json);
  return json;
}

void sendHttpCommand(const String& request) {
  if (request.length() > MAX_SERIAL_LINE) {
    addCorsHeaders();
    webServer.send(413, "application/json", "{\"ok\":false,\"error\":\"request too large\"}");
    return;
  }
  const String response = runCommandForReply(request);
  addCorsHeaders();
  webServer.send(200, "application/json", response);
}

void beginHttpServer() {
  webServer.on("/health", HTTP_GET, []() {
    addCorsHeaders();
    webServer.send(200, "application/json", "{\"ok\":true,\"device\":\"Lightstick-N16R8\"}");
  });
  webServer.on("/api/info", HTTP_GET, []() { sendHttpCommand(makeSimpleRequest("GET_INFO")); });
  webServer.on("/api/status", HTTP_GET, []() { sendHttpCommand(makeSimpleRequest("GET_STATUS")); });
  webServer.on("/api/commands", HTTP_GET, []() { sendHttpCommand(makeSimpleRequest("GET_COMMANDS")); });
  webServer.on("/api/command", HTTP_POST, []() { sendHttpCommand(webServer.arg("plain")); });
  webServer.on("/api/command", HTTP_OPTIONS, []() {
    addCorsHeaders();
    webServer.send(204);
  });
  webServer.onNotFound([]() {
    addCorsHeaders();
    webServer.send(404, "application/json", "{\"ok\":false,\"error\":\"not found\"}");
  });
  webServer.begin();
}

void stopNetworkDiscovery() {
  if (discoveryActive) {
    discoveryUdp.stop();
    discoveryActive = false;
  }
  if (mdnsActive) {
    MDNS.end();
    mdnsActive = false;
  }
}

void startNetworkDiscovery() {
  if (!mdnsActive && MDNS.begin(WIFI_HOSTNAME)) {
    MDNS.addService("http", "tcp", 80);
    mdnsActive = true;
  }
  if (!discoveryActive) {
    discoveryActive = discoveryUdp.begin(DISCOVERY_PORT) == 1;
  }
}

void startRecoveryAp() {
  if (recoveryApActive) return;
  WiFi.mode(WIFI_AP_STA);
  recoveryApActive = WiFi.softAP(WIFI_RECOVERY_SSID, nullptr, 6, false, 2);
}

void stopRecoveryAp() {
  if (!recoveryApActive) return;
  WiFi.softAPdisconnect(true);
  recoveryApActive = false;
  WiFi.mode(WIFI_STA);
}

void serviceWifi() {
  const bool connected = WiFi.status() == WL_CONNECTED;
  if (connected) {
    if (recoveryApActive) stopRecoveryAp();
    if (!wifiWasConnected) startNetworkDiscovery();
  } else {
    if (wifiWasConnected) stopNetworkDiscovery();
    if (!recoveryApActive && millis() - wifiStartedAtMs >= WIFI_RECOVERY_DELAY_MS) {
      startRecoveryAp();
    }
    if (!wifiSsid.isEmpty() && millis() - wifiLastReconnectMs >= WIFI_RECONNECT_INTERVAL_MS) {
      wifiLastReconnectMs = millis();
      ++wifiReconnectCount;
      WiFi.reconnect();
    }
  }
  wifiWasConnected = connected;
}

void sendUdpDatagram(const IPAddress& ip, uint16_t port, const String& payload) {
  discoveryUdp.beginPacket(ip, port);
  discoveryUdp.write(reinterpret_cast<const uint8_t*>(payload.c_str()), payload.length());
  discoveryUdp.endPacket();
}

// 同一个端口处理两件事:
//   1) LIGHTSTICK_DISCOVER -> 回设备信息 (原有行为, 未改动)
//   2) 以 { 开头的 JSON   -> 当成命令跑, 同步结果回给发送方,
//      之后的 TX_COMPLETE 等异步事件由 sendUdpResponse() 回到同一端。
void serviceDiscovery() {
  if (!discoveryActive) return;
  static char packet[UDP_MAX_COMMAND + 1];
  int packetSize = 0;
  while ((packetSize = discoveryUdp.parsePacket()) > 0) {
    const IPAddress remoteIp = discoveryUdp.remoteIP();
    const uint16_t remotePort = discoveryUdp.remotePort();
    const bool oversized = packetSize > static_cast<int>(UDP_MAX_COMMAND);
    const int bytesRead = discoveryUdp.read(
      packet, min(packetSize, static_cast<int>(sizeof(packet) - 1)));
    if (bytesRead <= 0) continue;
    packet[bytesRead] = '\0';
    String message(packet);
    message.trim();
    if (message.isEmpty()) continue;

    if (message == DISCOVERY_REQUEST) {
      JsonDocument response;
      response["device"] = "Lightstick-N16R8";
      response["firmware_version"] = FIRMWARE_VERSION;
      response["boot_id"] = bootId;
      response["ip"] = WiFi.localIP().toString();
      response["hostname"] = WIFI_HOSTNAME;
      response["mdns"] = String(WIFI_HOSTNAME) + ".local";
      response["http_port"] = 80;
      response["api_path"] = "/api/command";
      response["udp_command"] = true;
      String json;
      serializeJson(response, json);
      sendUdpDatagram(remoteIp, remotePort, json);
      continue;
    }

    if (message[0] != '{') continue;

    // 记下来源, 异步事件回这里
    udpPeerIp = remoteIp;
    udpPeerPort = remotePort;
    udpPeerActive = true;

    if (oversized) {
      sendUdpDatagram(remoteIp, remotePort,
        String("{\"ok\":false,\"error\":\"udp command exceeds ")
          + String(static_cast<unsigned>(UDP_MAX_COMMAND))
          + " bytes; use serial or HTTP for long payloads\"}" + (char)10);
      continue;
    }

    const String reply = runCommandForReply(message);
    if (reply.isEmpty()) continue;
    String framed = reply;
    framed += (char)10;
    sendUdpDatagram(remoteIp, remotePort, framed);
  }
}

void beginWifi() {
  WiFi.persistent(false);
  requestWifiConnection();
  beginHttpServer();
}

void beginBle() {
  BLEDevice::init(BLE_DEVICE_NAME);
  BLEDevice::setMTU(517);
  BLEServer* server = BLEDevice::createServer();
  server->setCallbacks(new LightstickBleServerCallbacks());
  BLEService* service = server->createService(BLE_SERVICE_UUID);
  BLECharacteristic* commandCharacteristic = service->createCharacteristic(
    BLE_COMMAND_UUID,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
  );
  commandCharacteristic->setCallbacks(new LightstickBleCommandCallbacks());
  bleResponseCharacteristic = service->createCharacteristic(
    BLE_RESPONSE_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY
  );
  bleResponseCharacteristic->addDescriptor(new BLE2902());
  bleResponseCharacteristic->setValue("ready\n");
  service->start();
  BLEAdvertising* advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(BLE_SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->start();
}

}  // namespace

void setup() {
  pinMode(PIN_GDO0, OUTPUT);
  digitalWrite(PIN_GDO0, LOW);
  pinMode(PIN_GDO2, INPUT);
  Serial.setRxBufferSize(32768);
  Serial.begin(921600);
  serialLine.reserve(MAX_SERIAL_LINE);
  serialMutex = xSemaphoreCreateMutex();
  commandMutex = xSemaphoreCreateMutex();
  txResultMutex = xSemaphoreCreateMutex();
  pulseRmtMutex = xSemaphoreCreateMutex();
  pulseQueue = xQueueCreate(1, sizeof(PulseTaskConfig*));
  bleCommandQueue = xQueueCreate(4, sizeof(String*));
  if (pulseQueue != nullptr) {
    xTaskCreatePinnedToCore(pulseTask, "pulse-tx", 8192, nullptr, 2, nullptr, 0);
  }

  preferences.begin("lightstick", false);
  loadReceiverConfig();
  loadRecordingConfig();
  loadWifiConfig();
  loadSignalProfiles();
  bootId = String("boot-") + String(static_cast<uint32_t>(ESP.getEfuseMac() >> 32), HEX)
    + '-' + String(esp_random(), HEX);

  radio.setSpiPin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CSN);
  radio.setGDO(PIN_GDO0, PIN_GDO2);
  radio.Init();
  radioFound = radio.getCC1101();
  String radioPrepareError;
  if (radioFound) prepareAsyncReceiver(radioPrepareError);
  idleRadio();

  String recorderError;
  recorder.begin(PIN_GDO0, recorderError);
  beginWifi();
  if (BLE_ENABLED) beginBle();

  JsonDocument boot;
  boot["event"] = "BOOT";
  boot["hardware_profile"] = "ESP32-S3-N16R8";
  boot["flash_size"] = ESP.getFlashChipSize();
  boot["psram_found"] = psramFound();
  boot["psram_size"] = ESP.getPsramSize();
  boot["cc1101_found"] = radioFound;
  boot["cc1101_calibrated"] = radioCalibrated;
  boot["cc1101_calibration_error"] = radioCalibrationError;
  boot["boot_id"] = bootId;
  JsonObject wifi = boot["wifi"].to<JsonObject>();
  addWifiStatus(wifi);
  boot["usb_error"] = recorderError;
  writeSerialJson(boot);
}

void loop() {
  serviceWifi();
  serviceDiscovery();
  webServer.handleClient();
  while (Serial.available() > 0) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\n') {
      serialLine.trim();
      if (!serialLine.isEmpty()) runSerialCommand(serialLine);
      serialLine = "";
    } else if (character != '\r') {
      if (serialLine.length() < MAX_SERIAL_LINE) {
        serialLine += character;
      } else {
        serialLine = "";
        respondError("", "serial command exceeds 24576 bytes");
      }
    }
  }

  String* bleCommand = nullptr;
  if (bleCommandQueue != nullptr && xQueueReceive(bleCommandQueue, &bleCommand, 0) == pdTRUE) {
    if (bleCommand != nullptr) {
      sendBleResponse(runCommandForReply(*bleCommand));
      delete bleCommand;
    }
  }
  String automaticStopError;
  if (recorder.serviceAutomaticStop(automaticStopError)) {
    if (radioFound && !txBusy) idleRadio();
    JsonDocument event;
    event["event"] = "RECORDING_STOPPED";
    event["automatic"] = true;
    if (!automaticStopError.isEmpty()) event["error"] = automaticStopError;
    JsonObject storage = event["storage"].to<JsonObject>();
    addRecorderStatus(storage);
    writeSerialJson(event);
  }
  delay(1);
}
