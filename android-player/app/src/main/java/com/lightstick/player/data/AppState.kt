package com.lightstick.player.data

/** 连接界面状态。 */
data class ConnectionUi(
    val kind: TransportKind = TransportKind.BLE,
    val address: String = "",
    val state: ConnState = ConnState.Disconnected,
    val label: String = "",
    val lastError: String? = null,
)

enum class ConnState { Disconnected, Connecting, Connected }

/** 设备信息(GET_INFO 的部分字段)。 */
data class DeviceInfo(
    val firmwareVersion: String = "",
    val chip: String = "",
    val cc1101Found: Boolean = false,
    val freeHeap: Int = 0,
    val psramFound: Boolean = false,
    val psramSize: Int = 0,
    val wifiConnected: Boolean = false,
    val wifiSsid: String = "",
    val ip: String = "",
    val mdns: String = "",
    val raw: String = "",
)

/** 播放进度状态。 */
data class PlaybackState(
    val playing: Boolean = false,
    val paused: Boolean = false,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
    val framesSent: Int = 0,
    val framesDropped: Int = 0,
    val intervalMs: Int = 0,
    val finished: Boolean = false,
) {
    val progress: Float
        get() = if (durationMs <= 0L) 0f else (positionMs.toFloat() / durationMs).coerceIn(0f, 1f)
}
