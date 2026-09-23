package com.lightstick.player.transport

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.lightstick.player.data.TransportKind
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException

/**
 * Wi-Fi UDP。
 *
 * 板子的 4210 端口同时做两件事(见固件里的 serviceDiscovery):
 *  - 收到 LIGHTSTICK_DISCOVER 就回一条设备信息 JSON
 *  - 收到以 { 开头的数据包就当 JSON 命令跑, 结果回到来源端口
 *
 * Android 上的坑: 广播必须走 Wi-Fi。手机同时开着移动数据时, 255.255.255.255 可能
 * 从蜂窝网出去, 所以这里显式把 socket 绑到 WIFI 传输的 Network 上。
 */
class UdpTransport(
    private val context: Context,
    private val scope: CoroutineScope,
    private var host: String? = null,
    private val port: Int = TransportDefaults.UDP_PORT,
) : Transport() {

    override val kind = TransportKind.UDP

    /** 走广播发现时拿到的设备信息。 */
    var deviceInfo: JSONObject? = null
        private set

    private var socket: DatagramSocket? = null
    private var receiveJob: Job? = null
    private var resolved: InetAddress? = null
    private var connected = false
    private var failures = 0L

    /** 只保留最新一条待发命令, 和 PC 端一致。 */
    @Volatile
    private var pending: ByteArray? = null

    override fun describe(): String {
        return "udp " + (host ?: "?") + ":" + port
    }

    override suspend fun open() = withContext(Dispatchers.IO) {
        if (host.isNullOrBlank()) {
            log("UDP: 广播发现板子…")
            val info = discover()
            if (info == null) {
                throw IllegalStateException(
                    "UDP 发现失败: 没有收到回应。确认手机和板子在同一个 Wi-Fi, " +
                        "或在地址栏直接填板子的 IP。"
                )
            }
            deviceInfo = info
            val found = info.optString("ip")
            host = if (found.isNotEmpty()) found else info.optString("_addr")
            log("UDP: 发现 " + info.optString("hostname", "?") + " @ " + host)
        }
        resolved = InetAddress.getByName(host)
        val sock = DatagramSocket()
        bindToWifi(sock)
        socket = sock
        connected = true
        emitState(true)
        receiveJob = scope.launch(Dispatchers.IO) { sendLoop(sock) }
    }

    /** 把 socket 绑到 Wi-Fi 网络, 避免广播从蜂窝网出去。 */
    private fun bindToWifi(sock: DatagramSocket) {
        try {
            val cm = context.getSystemService(ConnectivityManager::class.java)
            for (network in cm.allNetworks) {
                val caps = cm.getNetworkCapabilities(network) ?: continue
                if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                    network.bindSocket(sock)
                    log("UDP: socket 已绑定到 Wi-Fi")
                    return
                }
            }
            log("UDP: 没找到 Wi-Fi 网络, 用系统默认路由")
        } catch (e: Exception) {
            log("UDP: 绑定 Wi-Fi 失败(" + e.message + "), 用系统默认路由")
        }
    }

    /** 广播发现。重发几次, 并同时打全局广播和回环(代理/TUN 网卡会吃掉广播)。 */
    suspend fun discover(timeoutMs: Long = 2000L): JSONObject? = withContext(Dispatchers.IO) {
        val sock = DatagramSocket()
        try {
            sock.broadcast = true
            bindToWifi(sock)
            sock.soTimeout = 200
            val payload = TransportDefaults.DISCOVER_REQUEST.toByteArray(Charsets.UTF_8)
            val targets = listOf("255.255.255.255", "127.0.0.1")
            val deadline = System.currentTimeMillis() + timeoutMs
            var nextSend = 0L
            val buffer = ByteArray(2048)
            while (System.currentTimeMillis() < deadline) {
                val now = System.currentTimeMillis()
                if (now >= nextSend) {
                    for (target in targets) {
                        try {
                            sock.send(
                                DatagramPacket(
                                    payload, payload.size,
                                    InetAddress.getByName(target), port
                                )
                            )
                        } catch (_: Exception) {
                            // 某个目标发不出去无所谓, 继续试下一个
                        }
                    }
                    nextSend = now + 400
                }
                try {
                    val packet = DatagramPacket(buffer, buffer.size)
                    sock.receive(packet)
                    val text = String(packet.data, 0, packet.length, Charsets.UTF_8).trim()
                    if (!text.startsWith("{")) continue
                    val json = JSONObject(text)
                    if (json.has("device")) {
                        json.put("_addr", packet.address.hostAddress ?: "")
                        return@withContext json
                    }
                } catch (_: SocketTimeoutException) {
                } catch (_: Exception) {
                }
            }
            null
        } finally {
            sock.close()
        }
    }

    override fun send(json: String) {
        val bytes = (json + 10.toChar()).toByteArray(Charsets.UTF_8)
        if (bytes.size > TransportDefaults.UDP_MAX_COMMAND) {
            log("命令 " + bytes.size + " 字节, 超过 UDP 单包上限, 已丢弃")
            return
        }
        pending = bytes
    }

    private suspend fun sendLoop(sock: DatagramSocket) {
        val address = resolved ?: return
        val target = InetSocketAddress(address, port)
        val buffer = ByteArray(4096)
        while (scope.isActive && connected) {
            val payload = pending
            pending = null
            if (payload != null) {
                try {
                    sock.send(DatagramPacket(payload, payload.size, target))
                    failures = 0L
                } catch (e: Exception) {
                    // 同一个错误别每帧刷一次
                    if (failures % 40L == 0L) log("UDP 发送失败: " + e.message)
                    failures++
                }
            }
            try {
                sock.soTimeout = 30
                val packet = DatagramPacket(buffer, buffer.size)
                sock.receive(packet)
                val text = String(packet.data, 0, packet.length, Charsets.UTF_8).trim()
                for (line in text.split(10.toChar())) {
                    val trimmed = line.trim()
                    if (trimmed.startsWith("{")) emitReply(trimmed)
                }
            } catch (_: SocketTimeoutException) {
            } catch (_: Exception) {
            }
            delay(3)
        }
    }

    override fun isConnected(): Boolean = connected

    override fun close() {
        connected = false
        receiveJob?.cancel()
        receiveJob = null
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        emitState(false)
    }
}
