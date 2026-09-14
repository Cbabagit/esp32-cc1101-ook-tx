// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 lightstick-control contributors
#pragma once

#include <Arduino.h>

#include <vector>

struct ReceiverConfig {
  uint32_t frequencyHz = 433920000;
  uint32_t bandwidthHz = 203000;
  uint8_t modulation = 2;
  float dataRateKbaud = 4.8f;
};

enum class RecordingEdgeMode : uint8_t {
  Both = 0,
  Rising = 1,
  Falling = 2,
};

struct RecordingConfig {
  RecordingEdgeMode edgeMode = RecordingEdgeMode::Both;
  uint32_t minEdgeIntervalUs = 0;
  uint32_t maxDurationMs = 0;
  uint64_t maxEdges = 0;
  uint16_t clientChunkRecords = 512;
  uint8_t bufferStopThresholdPercent = 90;
  bool stopOnBufferOverflow = true;
};

struct RecordingFileInfo {
  String name;
  uint64_t sizeBytes = 0;
};

struct RecorderSnapshot {
  bool psramReady = false;
  size_t psramBufferBytes = 0;
  size_t psramBufferUsedBytes = 0;
  bool usbHostReady = false;
  bool usbMounted = false;
  uint64_t usbTotalBytes = 0;
  uint64_t usbFreeBytes = 0;
  bool recording = false;
  String recordingName;
  String currentFile;
  uint32_t segmentIndex = 0;
  uint64_t elapsedMs = 0;
  uint64_t capturedEdges = 0;
  uint64_t writtenEdges = 0;
  uint64_t droppedIsrEdges = 0;
  uint64_t droppedPsramEdges = 0;
  uint64_t filteredEdges = 0;
  String recordingTarget = "none";
  bool clientRecordingAvailable = false;
  bool clientEndOfStream = false;
  String clientSessionId;
  uint64_t clientAcknowledgedRecords = 0;
  uint64_t clientPendingRecords = 0;
  uint64_t clientBufferedRecords = 0;
  ReceiverConfig activeReceiverConfig;
  RecordingConfig activeRecordingConfig;
  String clientHeaderBase64;
  String stopReason;
  String lastError;
};

struct ClientRecordingStart {
  String sessionId;
  String recordingName;
  String headerBase64;
};

struct ClientRecordingChunk {
  String sessionId;
  String recordingName;
  uint64_t sequenceStart = 0;
  uint64_t sequenceEnd = 0;
  uint64_t offsetBytes = 0;
  uint64_t nextOffsetBytes = 0;
  uint32_t recordCount = 0;
  uint32_t dataCrc32 = 0;
  String dataBase64;
  bool active = false;
  bool endOfStream = false;
};

class LightstickRecorder {
 public:
  bool begin(uint8_t inputPin, String& error);
  bool start(
    const String& requestedName,
    const ReceiverConfig& config,
    const RecordingConfig& recordingConfig,
    uint64_t utcEpochMs,
    String& error
  );
  bool startClient(
    const String& requestedName,
    const ReceiverConfig& config,
    const RecordingConfig& recordingConfig,
    uint64_t utcEpochMs,
    ClientRecordingStart& result,
    String& error
  );
  bool stop(String& error, const String& reason = "manual");
  bool stopClient(const String& sessionId, String& error);
  bool readClient(
    const String& sessionId,
    uint64_t acknowledgement,
    size_t maximumRecords,
    ClientRecordingChunk& result,
    String& error
  );
  bool serviceAutomaticStop(String& error);
  RecorderSnapshot snapshot() const;
  std::vector<RecordingFileInfo> listFiles(String& error) const;
  bool deleteFile(const String& name, String& error);
};

extern LightstickRecorder recorder;
