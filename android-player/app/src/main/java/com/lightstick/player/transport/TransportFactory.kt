package com.lightstick.player.transport

import android.content.Context
import com.lightstick.player.data.TransportKind
import kotlinx.coroutines.CoroutineScope

/** 按连接方式建传输实例。address 的含义随 kind 变化。 */
object TransportFactory {

    fun create(
        context: Context,
        scope: CoroutineScope,
        kind: TransportKind,
        address: String,
    ): Transport {
        val trimmed = address.trim()
        return when (kind) {
            TransportKind.UDP -> UdpTransport(
                context = context,
                scope = scope,
                host = trimmed.ifEmpty { null },
            )

            TransportKind.HTTP -> {
                if (trimmed.isEmpty()) {
                    throw IllegalArgumentException("HTTP 需要填板子的 IP")
                }
                HttpTransport(scope = scope, host = trimmed)
            }

            TransportKind.BLE -> BleTransport(
                context = context,
                scope = scope,
                deviceName = trimmed.ifEmpty { null },
            )

            TransportKind.USB -> UsbSerialTransport()
        }
    }
}
