package com.lightstick.player.transport

import com.lightstick.player.data.TransportKind
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL

/**
 * Wi-Fi HTTP。
 *
 * 板子跑了一个 WebServer, 命令走 POST /api/command, 请求体就是那行 JSON,
 * 响应体是同一行 JSON。比 UDP 可靠, 但每条命令一个 TCP 往返, 延迟更大,
 * 所以只在 UDP 不通或需要绝对可靠时用。
 */
class HttpTransport(
    private val scope: CoroutineScope,
    private var host: String,
    private val port: Int = TransportDefaults.HTTP_PORT,
) : Transport() {

    override val kind = TransportKind.HTTP

    @Volatile
    private var pending: String? = null

    private var job: Job? = null

    @Volatile
    private var connected = false

    override fun describe(): String = "http " + host + ":" + port

    override suspend fun open() = withContext(Dispatchers.IO) {
        val url = URL("http://" + host + ":" + port + "/health")
        try {
            val connection = url.openConnection() as HttpURLConnection
            connection.connectTimeout = 4000
            connection.readTimeout = 4000
            connection.requestMethod = "GET"
            val code = connection.responseCode
            connection.disconnect()
            if (code !in 200..299) {
                throw IllegalStateException("板子回了 HTTP " + code)
            }
        } catch (e: Exception) {
            throw IllegalStateException(
                "连不上 http://" + host + ":" + port + " —— " + (e.message ?: "未知错误")
            )
        }
        connected = true
        emitState(true)
        job = scope.launch(Dispatchers.IO) { loop() }
    }

    override fun send(json: String) {
        pending = json
    }

    private suspend fun loop() {
        var failures = 0L
        while (scope.isActive && connected) {
            val body = pending
            pending = null
            if (body != null) {
                try {
                    val response = post(body)
                    failures = 0L
                    if (response != null) emitReply(response)
                } catch (e: Exception) {
                    if (failures % 10L == 0L) log("HTTP 发送失败: " + e.message)
                    failures++
                }
            }
            delay(10)
        }
    }

    private fun post(body: String): String? {
        val url = URL("http://" + host + ":" + port + TransportDefaults.HTTP_COMMAND_PATH)
        val connection = url.openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 4000
            connection.readTimeout = 6000
            connection.requestMethod = "POST"
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            OutputStreamWriter(connection.outputStream, Charsets.UTF_8).use { writer ->
                writer.write(body)
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }
            if (code !in 200..299) {
                throw IllegalStateException("HTTP " + code + ": " + (text ?: ""))
            }
            return text?.trim()?.takeIf { it.startsWith("{") }
        } finally {
            connection.disconnect()
        }
    }

    override fun isConnected(): Boolean = connected

    override fun close() {
        connected = false
        job?.cancel()
        job = null
        emitState(false)
    }
}
