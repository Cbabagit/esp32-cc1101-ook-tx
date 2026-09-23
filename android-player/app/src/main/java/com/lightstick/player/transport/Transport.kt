package com.lightstick.player.transport

import com.lightstick.player.data.TransportKind

/** 固件里写死的参数, 改固件时这里要同步。 */
object TransportDefaults {
    const val UDP_PORT = 4210
    const val UDP_MAX_COMMAND = 1400
    const val HTTP_PORT = 80
    const val HTTP_COMMAND_PATH = "/api/command"

    const val DISCOVER_REQUEST = "LIGHTSTICK_DISCOVER"

    const val BLE_DEVICE_NAME = "Lightstick N16R8"
    const val BLE_SERVICE_UUID = "8f7a0001-4c53-4331-9638-53334e313652"
    const val BLE_COMMAND_UUID = "8f7a0002-4c53-4331-9638-53334e313652"
    const val BLE_RESPONSE_UUID = "8f7a0003-4c53-4331-9638-53334e313652"
}

/**
 * 传输层公共接口。
 *
 * 几种连接(USB 串口 / Wi-Fi UDP / HTTP / BLE)对上层是同一套接口: 发一行 JSON, 收一行 JSON。
 *
 * 和 PC 端一致: send 不阻塞调用方(内部排队 + 只保留最新一条), 免得界面卡顿。
 */
abstract class Transport {

    abstract val kind: TransportKind

    /** 给界面显示的一句话描述, 如 "udp 192.168.1.57:4210"。 */
    abstract fun describe(): String

    /** 建立连接。失败抛异常, 由调用方展示。 */
    abstract suspend fun open()

    /** 发一条命令(完整的一行 JSON, 不含换行)。非阻塞。 */
    abstract fun send(json: String)

    open fun close() {}

    open fun isConnected(): Boolean = true

    /** 收到设备响应(一行 JSON)时回调。不保证在主线程。 */
    var onReply: ((String) -> Unit)? = null

    /** 链路状态变化回调 (connected, label)。 */
    var onState: ((Boolean, String) -> Unit)? = null

    /** 供界面日志用的文本回调。 */
    var onLog: ((String) -> Unit)? = null

    protected fun log(message: String) {
        onLog?.invoke(message)
    }

    protected fun emitReply(line: String) {
        onReply?.invoke(line)
    }

    protected fun emitState(connected: Boolean, label: String = "") {
        onState?.invoke(connected, label.ifEmpty { describe() })
    }
}
