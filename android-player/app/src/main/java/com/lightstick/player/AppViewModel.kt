package com.lightstick.player

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.lightstick.player.data.Channel
import com.lightstick.player.data.ConnState
import com.lightstick.player.data.ConnectionUi
import com.lightstick.player.data.DeviceInfo
import com.lightstick.player.data.Mode
import com.lightstick.player.data.PlaybackState
import com.lightstick.player.data.Sequence
import com.lightstick.player.data.TransportKind
import com.lightstick.player.data.TxParams
import com.lightstick.player.protocol.Protocol
import com.lightstick.player.transport.Transport
import com.lightstick.player.transport.TransportFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader

/**
 * 全局状态与动作。界面只读 StateFlow, 所有副作用都在这里。
 *
 * 设计上刻意保持"傻瓜": 一个 VM 管住连接、通道、序列、日志四块, 不再拆细,
 * 因为整个 App 的复杂度主要来自协议和连接, 不在状态管理。
 */
class AppViewModel(app: Application) : AndroidViewModel(app) {

    private val _connection = MutableStateFlow(ConnectionUi())
    val connection: StateFlow<ConnectionUi> = _connection.asStateFlow()

    private val _logs = MutableStateFlow<List<String>>(emptyList())
    val logs: StateFlow<List<String>> = _logs.asStateFlow()

    private val _device = MutableStateFlow<DeviceInfo?>(null)
    val device: StateFlow<DeviceInfo?> = _device.asStateFlow()

    private val _params = MutableStateFlow(TxParams())
    val params: StateFlow<TxParams> = _params.asStateFlow()

    private val _channels = MutableStateFlow(List(Protocol.SLOT_WORDS) { Channel() })
    val channels: StateFlow<List<Channel>> = _channels.asStateFlow()

    private val _selected = MutableStateFlow(setOf(0))
    val selected: StateFlow<Set<Int>> = _selected.asStateFlow()

    private val _sequence = MutableStateFlow<Sequence?>(null)
    val sequence: StateFlow<Sequence?> = _sequence.asStateFlow()

    private val _playback = MutableStateFlow(PlaybackState())
    val playback: StateFlow<PlaybackState> = _playback.asStateFlow()

    private val _lastReply = MutableStateFlow("")
    val lastReply: StateFlow<String> = _lastReply.asStateFlow()

    private val _terminalInput = MutableStateFlow("")
    val terminalInput: StateFlow<String> = _terminalInput.asStateFlow()

    private var transport: Transport? = null
    private var playbackJob: Job? = null
    private var pendingResult: String? = null
    private var seqCounter = 0L

    // ---- 日志 ----

    fun log(message: String) {
        val stamp = android.text.format.DateFormat.format("HH:mm:ss", System.currentTimeMillis())
        _logs.value = (_logs.value + ("[" + stamp + "] " + message)).takeLast(300)
    }

    fun clearLogs() {
        _logs.value = emptyList()
    }

    // ---- 连接 ----

    fun setTransportKind(kind: TransportKind) {
        _connection.value = _connection.value.copy(kind = kind, lastError = null)
    }

    fun setAddress(address: String) {
        _connection.value = _connection.value.copy(address = address)
    }

    fun connect() {
        val current = _connection.value
        if (current.state == ConnState.Connecting) return
        disconnect(quiet = true)
        _connection.value = current.copy(state = ConnState.Connecting, lastError = null)
        viewModelScope.launch {
            try {
                val created = TransportFactory.create(
                    context = getApplication(),
                    scope = viewModelScope,
                    kind = current.kind,
                    address = current.address,
                )
                created.onLog = { message -> viewModelScope.launch { log(message) } }
                created.onReply = { line -> viewModelScope.launch { handleReply(line) } }
                created.onState = { connected, label ->
                    viewModelScope.launch {
                        _connection.value = _connection.value.copy(
                            state = if (connected) ConnState.Connected else ConnState.Disconnected,
                            label = label,
                        )
                        if (!connected) {
                            log("连接已断开: " + label)
                            // 必须把 GATT 关掉: 只把状态改成"未连接"的话,
                            // 蓝牙栈里的那条链路会一直占着, 板子也以为还连着。
                            if (transport === created) {
                                try {
                                    created.close()
                                } catch (_: Exception) {
                                }
                                transport = null
                            }
                        }
                    }
                }
                withContext(Dispatchers.IO) { created.open() }
                transport = created
                _connection.value = _connection.value.copy(
                    state = ConnState.Connected,
                    label = created.describe(),
                )
                log("已连接: " + created.describe())
                // 连上先问一次设备信息
                send(Protocol.simpleCommand("GET_INFO", nextId()))
            } catch (e: Exception) {
                val message = e.message ?: e.toString()
                _connection.value = _connection.value.copy(
                    state = ConnState.Disconnected,
                    lastError = message,
                )
                log("连接失败: " + message)
                try {
                    transport?.close()
                } catch (_: Exception) {
                }
                transport = null
            }
        }
    }

    fun disconnect(quiet: Boolean = false) {
        try {
            transport?.close()
        } catch (_: Exception) {
        }
        transport = null
        if (!quiet) {
            _connection.value = _connection.value.copy(state = ConnState.Disconnected, label = "")
            log("已断开")
        }
    }

    fun nextId(): String {
        seqCounter++
        return "app-" + seqCounter
    }

    fun send(json: String) {
        val tx = transport
        if (tx == null || !tx.isConnected()) {
            log("未连接, 命令未发出")
            return
        }
        pendingResult = json
        tx.send(json)
    }

    fun sendTerminal() {
        val text = _terminalInput.value.trim()
        if (text.isEmpty()) return
        send(text)
        _terminalInput.value = ""
    }

    fun setTerminalInput(text: String) {
        _terminalInput.value = text
    }

    private fun handleReply(line: String) {
        _lastReply.value = line
        try {
            val json = JSONObject(line)
            if (json.optString("event") == "BOOT") {
                log("板子重启: " + json.optString("hardware_profile"))
                applyInfo(json)
                return
            }
            val command = json.optString("cmd")
            if (command == "GET_INFO") {
                applyInfo(json.optJSONObject("result") ?: json)
            }
            val status = json.optString("status")
            if (status.isNotEmpty() && status != "success") {
                log("设备回复: " + status)
            }
            val error = json.optString("error")
            if (error.isNotEmpty()) log("设备报错: " + error)
        } catch (_: Exception) {
            log("收到无法解析的响应: " + line.take(120))
        }
    }

    private fun applyInfo(json: JSONObject) {
        val pins = json.optJSONObject("pins")
        val wifi = json.optJSONObject("wifi") ?: json.optJSONObject("result")?.optJSONObject("wifi")
        _device.value = DeviceInfo(
            firmwareVersion = json.optString("version", json.optString("firmware_version")),
            chip = json.optString("chip"),
            cc1101Found = json.optBoolean("cc1101_found", pins != null),
            freeHeap = json.optInt("free_heap"),
            psramFound = json.optBoolean("psram_found"),
            psramSize = json.optInt("psram_size"),
            wifiConnected = wifi?.optBoolean("connected") ?: false,
            wifiSsid = wifi?.optString("ssid") ?: "",
            ip = wifi?.optString("ip") ?: "",
            mdns = wifi?.optString("mdns") ?: "",
            raw = json.toString(),
        )
    }

    // ---- 发射参数 ----

    fun updateParams(block: (TxParams) -> TxParams) {
        _params.value = block(_params.value)
    }

    fun setMode(mode: Mode) = updateParams { it.copy(mode = mode) }

    // ---- 通道与手动控制 ----

    fun toggleSlot(index: Int) {
        val current = _selected.value
        _selected.value = if (current.contains(index)) {
            if (current.size == 1) current else current - index
        } else {
            current + index
        }
    }

    fun selectAllSlots() {
        _selected.value = (0 until Protocol.SLOT_WORDS).toSet()
    }

    fun setChannel(index: Int, channel: Channel) {
        val list = _channels.value.toMutableList()
        if (index in list.indices) {
            list[index] = channel
            _channels.value = list
        }
    }

    /** 把颜色应用到选中的槽(拖动时只改本地, 松手才发命令)。 */
    fun applyColorLocally(r: Int, g: Int, b: Int) {
        val list = _channels.value.toMutableList()
        for (index in _selected.value) {
            if (index in list.indices) list[index] = list[index].copy(r = r, g = g, b = b)
        }
        _channels.value = list
    }

    fun applyFunctionLocally(function: Int) {
        val list = _channels.value.toMutableList()
        for (index in _selected.value) {
            if (index in list.indices) list[index] = list[index].copy(function = function)
        }
        _channels.value = list
    }

    /** 把当前通道状态按所选模式发出去。 */
    fun pushCurrentState() {
        val p = _params.value
        val id = nextId()
        val json = when (p.mode) {
            Mode.D8 -> Protocol.txD8Command(
                _channels.value, id, p.frequencyHz, p.powerDbm, p.burstFrames, p.bitUs, p.gapUs,
            )
            Mode.C0 -> Protocol.c0Command(
                c0Colors(), id, p.frequencyHz, p.powerDbm, p.repeat, p.bitUs, p.gapUs,
            )
            Mode.ZONE -> Protocol.zoneCommand(
                0xFF, 0xFF, Protocol.FUNCTION_TO_STATE[_channels.value[0].function] ?: 0x01,
                Protocol.nearestPalette(_channels.value[0].r, _channels.value[0].g, _channels.value[0].b),
                id, p.frequencyHz, p.powerDbm, p.repeat, p.bitUs, p.gapUs,
            )
        }
        send(json)
    }

    private fun c0Colors(): List<Int> {
        val colors = _channels.value.map {
            Protocol.nearestPalette(it.r, it.g, it.b)
        }.toMutableList()
        while (colors.size < 10) colors.add(colors.lastOrNull() ?: 0)
        return colors.take(10)
    }

    /** 全灭。按黑场走 00 命令 state=0, 不走调色板(否则会被量化成深绿)。 */
    fun blackout() {
        val p = _params.value
        val json = Protocol.zoneCommand(
            0xFF, 0xFF, 0x00, 0x00, nextId(), p.frequencyHz, p.powerDbm,
            p.repeat, p.bitUs, p.gapUs,
        )
        _channels.value = List(Protocol.SLOT_WORDS) { Channel() }
        send(json)
    }

    // ---- 序列 ----

    fun loadSequence(uri: Uri?) {
        if (uri == null) return
        viewModelScope.launch {
            try {
                val text = withContext(Dispatchers.IO) {
                    getApplication<Application>().contentResolver.openInputStream(uri)
                        ?.use { stream ->
                            BufferedReader(InputStreamReader(stream)).readText()
                        } ?: throw IllegalStateException("打不开文件")
                }
                val name = uri.lastPathSegment ?: "sequence.csv"
                val parsed = CsvParser.parse(text, name)
                _sequence.value = parsed
                _playback.value = PlaybackState(durationMs = parsed.durationMs)
                log("已加载 " + name + ": " + parsed.frames.size + " 帧, " +
                    parsed.channelCount + " 通道")
            } catch (e: Exception) {
                log("CSV 加载失败: " + (e.message ?: "未知错误"))
            }
        }
    }

    fun playSequence() {
        val seq = _sequence.value ?: return
        if (seq.frames.isEmpty()) return
        playbackJob?.cancel()
        val p = _params.value
        val interval = when (p.mode) {
            Mode.D8 -> Protocol.modeIntervalMs("d8", p.burstFrames, p.bitUs, p.gapUs)
            else -> if (p.repeat > 1) 46 * p.repeat else 46
        }
        _playback.value = _playback.value.copy(
            playing = true, paused = false, positionMs = 0, framesSent = 0,
            framesDropped = 0, intervalMs = if (p.throttle) interval else 0, finished = false,
        )
        playbackJob = viewModelScope.launch {
            val started = System.currentTimeMillis()
            var next = 0
            var nextOkAt = 0L
            var sent = 0
            var dropped = 0
            while (isActive && next < seq.frames.size) {
                val now = System.currentTimeMillis() - started
                var progressed = false
                while (next < seq.frames.size && seq.frames[next].timeMs <= now) {
                    if (_playback.value.intervalMs > 0 && now < nextOkAt) {
                        if (p.dropLate) {
                            // 跟不上就丢帧, 保持和视频/音乐同步
                            while (next < seq.frames.size && seq.frames[next].timeMs <= now) {
                                next++
                                dropped++
                            }
                        }
                        break
                    }
                    sendFrame(seq.frames[next])
                    next++
                    sent++
                    nextOkAt = now + _playback.value.intervalMs
                    progressed = true
                }
                _playback.value = _playback.value.copy(
                    positionMs = now.coerceAtMost(seq.durationMs),
                    framesSent = sent,
                    framesDropped = dropped,
                )
                if (!progressed) delay(5)
            }
            _playback.value = _playback.value.copy(
                playing = false, paused = false, finished = true,
                framesSent = sent, framesDropped = dropped, positionMs = seq.durationMs,
            )
            log("序列播放完成: 发送 " + sent + " 帧, 跳过 " + dropped + " 帧")
        }
    }

    private fun sendFrame(frame: com.lightstick.player.data.Frame) {
        val p = _params.value
        val id = nextId()
        val json = when (p.mode) {
            Mode.D8 -> Protocol.txD8Command(
                frame.channels, id, p.frequencyHz, p.powerDbm, p.burstFrames, p.bitUs, p.gapUs,
            )
            Mode.C0 -> {
                val colors = frame.channels.map {
                    Protocol.nearestPalette(it.r, it.g, it.b)
                }.toMutableList()
                while (colors.size < 10) colors.add(colors.lastOrNull() ?: 0)
                Protocol.c0Command(
                    colors.take(10), id, p.frequencyHz, p.powerDbm, p.repeat, p.bitUs, p.gapUs,
                )
            }
            Mode.ZONE -> Protocol.zoneCommand(
                0xFF, 0xFF, 0x01,
                Protocol.nearestPalette(
                    frame.channels.firstOrNull()?.r ?: 0,
                    frame.channels.firstOrNull()?.g ?: 0,
                    frame.channels.firstOrNull()?.b ?: 0,
                ),
                id, p.frequencyHz, p.powerDbm, p.repeat, p.bitUs, p.gapUs,
            )
        }
        send(json)
    }

    fun pauseSequence() {
        playbackJob?.cancel()
        playbackJob = null
        _playback.value = _playback.value.copy(playing = false, paused = true)
        log("已暂停")
    }

    fun stopSequence() {
        playbackJob?.cancel()
        playbackJob = null
        _playback.value = PlaybackState(durationMs = _sequence.value?.durationMs ?: 0L)
        log("已停止")
    }

    override fun onCleared() {
        playbackJob?.cancel()
        disconnect(quiet = true)
        super.onCleared()
    }
}
