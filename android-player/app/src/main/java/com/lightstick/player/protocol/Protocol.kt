package com.lightstick.player.protocol

import com.lightstick.player.data.Channel

/**
 * 空口协议的 Kotlin 实现, 与 PC 端 lightstick-player/protocol.py 一一对应。
 *
 * 改动这个文件时务必同步跑 ProtocolTest: 那里把 Python 侧 test_core.py 的期望值
 * 直接搬了过来(比如 D8 六帧爆发必须是 duration_us=785750 / pulse_count=2040),
 * 两端一旦不一致立刻会红。
 *
 * 注意: 帧构造(含校验和)是主机侧的事, 固件里那两处是占位符。
 */
object Protocol {

    // ---- 常量 ----
    const val CHECKSUM_SEED = 0x96
    const val SLOT_WORDS = 9
    const val SLOT_ROTL = 2

    const val CMD_ZONE = 0x00
    const val CMD_10COL = 0xC0
    const val CMD_PULSE = 0xA6
    const val CMD_UNLOCK = 0xDA
    const val CMD_SLOT = 0xD8

    /** 17bit 前导码, 原样发射。 */
    const val AIR_PREFIX = "11111111000011110"
    val D8_PHASES = intArrayOf(2, 1, 0, 2, 1, 0)
    const val D8_FRAME_INTERVAL_US = 131100
    const val AIR_GAP_US = 850
    const val BIT_US = 250

    /** 实测接收端在 200us 仍能解码, 190us 及以下不再解码。 */
    const val MIN_BIT_US = 200

    const val DEFAULT_FREQ_HZ = 433_920_000L
    const val DEFAULT_POWER_DBM = -20

    /** D8 function -> 00 命令的状态码。 */
    val FUNCTION_TO_STATE = mapOf(0 to 0x01, 1 to 0x02, 2 to 0x03, 3 to 0x04)

    /** 各模式一条命令的空口时长(ms), 用于限速。 */
    val MODE_INTERVAL_MS = mapOf("d8" to 786, "c0" to 380, "zone" to 380)

    private const val HEX_DIGITS = "0123456789ABCDEF"

    // ---- 纯助手 ----

    /** 校验和 = (0x96 + 前面所有帧字节之和) & 0xFF。 */
    fun checksum(data: ByteArray): Int {
        var sum = CHECKSUM_SEED
        for (b in data) sum = (sum + (b.toInt() and 0xFF)) and 0xFF
        return sum
    }

    /** 16bit 循环左移。 */
    fun rol16(value: Int, amount: Int): Int {
        val n = ((amount % 16) + 16) % 16
        if (n == 0) return value and 0xFFFF
        return (((value shl n) or (value ushr (16 - n))) and 0xFFFF)
    }

    /** 打包逻辑槽字: FFFF RRRR GGGG BBBB。 */
    fun slotWord(function: Int, r: Int, g: Int, b: Int): Int {
        return ((function and 0xF) shl 12) or ((r and 0xF) shl 8) or
            ((g and 0xF) shl 4) or (b and 0xF)
    }

    fun ByteArray.toHex(): String {
        val sb = StringBuilder(size * 2)
        for (b in this) {
            val v = b.toInt() and 0xFF
            sb.append(HEX_DIGITS[v shr 4]).append(HEX_DIGITS[v and 0xF])
        }
        return sb.toString()
    }

    // ---- 原始帧构造 (含校验) ----

    fun buildZoneFrame(m1: Int, m2: Int, x: Int = 0xFF, state: Int = 0x01, color: Int = 0x00): ByteArray {
        val body = byteArrayOf(
            CMD_ZONE.toByte(), (m1 and 0xFF).toByte(), (m2 and 0xFF).toByte(),
            (x and 0xFF).toByte(), (state and 0xFF).toByte(), (color and 0xFF).toByte(),
        )
        return body + byteArrayOf(checksum(body).toByte())
    }

    fun buildC0Frame(colors: List<Int>): ByteArray {
        require(colors.size == 10) { "C0 需要正好 10 个色码 (A-J)" }
        val body = ByteArray(6)
        body[0] = CMD_10COL.toByte()
        var index = 1
        var i = 0
        while (i < 10) {
            body[index++] = (((colors[i] and 0xF) shl 4) or (colors[i + 1] and 0xF)).toByte()
            i += 2
        }
        return body + byteArrayOf(checksum(body).toByte())
    }

    fun buildA6Frame(color: Int = 0x07): ByteArray {
        val body = byteArrayOf(CMD_PULSE.toByte(), (color and 0xF).toByte())
        return body + byteArrayOf(checksum(body).toByte())
    }

    fun buildDaFrame(m1: Int, m2: Int, x1: Int = 0xFF, x2: Int = 0x01, x3: Int = 0x00): ByteArray {
        val body = byteArrayOf(
            CMD_UNLOCK.toByte(), (m1 and 0xFF).toByte(), (m2 and 0xFF).toByte(),
            (x1 and 0xFF).toByte(), (x2 and 0xFF).toByte(), (x3 and 0xFF).toByte(),
        )
        return body + byteArrayOf(checksum(body).toByte())
    }

    /** D8 21 字节帧: D8 + 9 槽(ROL16 左移 2, 大端) + phase + 校验和。 */
    fun buildD8Frame(slots: IntArray, phase: Int = 2): ByteArray {
        require(slots.size == SLOT_WORDS) { "D8 需要正好 $SLOT_WORDS 个槽字" }
        val body = ByteArray(20)
        body[0] = CMD_SLOT.toByte()
        var index = 1
        for (slot in slots) {
            val w = rol16(slot, SLOT_ROTL)
            body[index++] = ((w shr 8) and 0xFF).toByte()
            body[index++] = (w and 0xFF).toByte()
        }
        body[19] = (phase and 0xFF).toByte()
        return body + byteArrayOf(checksum(body).toByte())
    }

    // ---- 空口编码 ----

    /** 帧字节 -> 空中 bit 串: 前导 17bit + 每数据位 0,1,d (MSB first)。 */
    fun frameToBits(frame: ByteArray): String {
        val sb = StringBuilder(AIR_PREFIX.length + frame.size * 24)
        sb.append(AIR_PREFIX)
        for (byte in frame) {
            for (i in 7 downTo 0) {
                val d = (byte.toInt() shr i) and 1
                sb.append(48.toChar()).append(49.toChar()).append((48 + d).toChar())
            }
        }
        return sb.toString()
    }

    /** bit 串 -> 游程编码的交替时长(us), 起始电平为高。 */
    fun bitsToDurations(bits: String, bitUs: Int = BIT_US): List<Int> {
        val out = ArrayList<Int>(bits.length / 2 + 2)
        if (bits.isEmpty()) return out
        var current = bits[0]
        var run = 0
        for (b in bits) {
            if (b == current) {
                run++
            } else {
                out.add(run * bitUs)
                current = b
                run = 1
            }
        }
        out.add(run * bitUs)
        return out
    }

    /** 一帧的空口时长(us) = 符号数 x 符号宽度。 */
    fun frameDurationUs(frame: ByteArray, bitUs: Int = BIT_US): Long {
        return (AIR_PREFIX.length + frame.size * 24).toLong() * bitUs
    }

    /**
     * D8 爆发的完整 durations(us)。
     *
     * burstFrames: 6 = 两个 phase 周期(默认, 最可靠, 空口 786ms)
     *              3 = 一个 2,1,0 周期(393ms)
     *              1 = 单帧(131ms)
     */
    fun buildD8Transaction(slots: IntArray, bitUs: Int = BIT_US, burstFrames: Int = 6): List<Int> {
        val n = burstFrames.coerceIn(1, D8_PHASES.size)
        val merged = ArrayList<Int>(n * 400)
        for (i in 0 until n) {
            val phase = D8_PHASES[i % D8_PHASES.size]
            val durations = bitsToDurations(frameToBits(buildD8Frame(slots, phase)), bitUs)
            if (i > 0) {
                val gap = D8_FRAME_INTERVAL_US - durations.sum()
                if (merged.size % 2 == 1) merged.add(gap) else merged[merged.size - 1] += gap
            }
            merged.addAll(durations)
        }
        return merged
    }

    /** 一条 TX_D8 命令占用的空口时长(us)。 */
    fun d8AirTimeUs(burstFrames: Int = 6, bitUs: Int = BIT_US, gapUs: Int = AIR_GAP_US): Long {
        val n = burstFrames.coerceIn(1, D8_PHASES.size)
        val symbols = AIR_PREFIX.length + 21 * 24
        return n.toLong() * symbols * bitUs + (n - 1).toLong() * gapUs
    }

    /** 某模式下一条命令的空口时长(ms), 用于限速。 */
    fun modeIntervalMs(mode: String, burstFrames: Int = 6, bitUs: Int = BIT_US, gapUs: Int = AIR_GAP_US): Int {
        if (mode == "d8") {
            return ((d8AirTimeUs(burstFrames, bitUs, gapUs) + 999) / 1000).toInt()
        }
        return MODE_INTERVAL_MS[mode] ?: 380
    }

    // ---- 调色板 ----

    private val PALETTE_HEX = intArrayOf(
        0xFF0000, 0x00B51A, 0x1878FF, 0xFF007C, 0xFFFFFF, 0xFFD400, 0x66CCFF, 0x00D878,
        0x8A4DFF, 0xFF6A00, 0xFF8AE0, 0x1B90FF, 0xFFF29A, 0x007B66, 0xFF5C5C, 0xF8FAFF,
    )

    /** RGB (0..15 每分量) -> 最近的 16 色调色板色码。 */
    fun nearestPalette(r: Int, g: Int, b: Int): Int {
        val r8 = r * 17
        val g8 = g * 17
        val b8 = b * 17
        var best = 0
        var bestDist = Int.MAX_VALUE
        for (code in PALETTE_HEX.indices) {
            val rgb = PALETTE_HEX[code]
            val dr = r8 - ((rgb shr 16) and 0xFF)
            val dg = g8 - ((rgb shr 8) and 0xFF)
            val db = b8 - (rgb and 0xFF)
            val dist = dr * dr + dg * dg + db * db
            if (dist < bestDist) {
                bestDist = dist
                best = code
            }
        }
        return best
    }

    fun isBlackout(type: String, channels: List<Channel>): Boolean {
        if (type.trim().lowercase() == "blackout") return true
        if (channels.isEmpty()) return true
        return channels.all { it.isEmpty }
    }

    fun d8Slots(channels: List<Channel>): IntArray {
        val slots = IntArray(SLOT_WORDS)
        for (i in 0 until minOf(channels.size, SLOT_WORDS)) {
            val c = channels[i]
            slots[i] = slotWord(c.function, c.r, c.g, c.b)
        }
        return slots
    }

    // ---- 命令构造 (JSON 字符串, 与 Python 端逐字节一致) ----

    fun txD8Command(
        channels: List<Channel>,
        requestId: String,
        frequencyHz: Long = DEFAULT_FREQ_HZ,
        powerDbm: Int = DEFAULT_POWER_DBM,
        burstFrames: Int = 6,
        bitUs: Int = BIT_US,
        gapUs: Int = AIR_GAP_US,
    ): String {
        val args = JsonWriter().obj()
            .putIntArray("slots", d8Slots(channels))
            .put("burst", burstFrames)
            .put("bit_us", bitUs)
            .put("gap_us", gapUs)
            .put("frequency_hz", frequencyHz)
            .put("power_dbm", powerDbm)
            .endObj()
            .toString()
        return JsonWriter().obj()
            .put("id", requestId)
            .put("cmd", "TX_D8")
            .putRaw("args", args)
            .endObj()
            .toString()
    }

    fun txBytesCommand(
        frame: ByteArray,
        requestId: String,
        frequencyHz: Long = DEFAULT_FREQ_HZ,
        powerDbm: Int = DEFAULT_POWER_DBM,
        repeat: Int = 1,
        bitUs: Int = BIT_US,
        gapUs: Int = 20000,
    ): String {
        val args = JsonWriter().obj()
            .put("data_hex", frame.toHex())
            .put("repeat", repeat)
            .put("bit_us", bitUs)
            .put("gap_us", gapUs)
            .put("frequency_hz", frequencyHz)
            .put("power_dbm", powerDbm)
            .endObj()
            .toString()
        return JsonWriter().obj()
            .put("id", requestId)
            .put("cmd", "TX_BYTES")
            .putRaw("args", args)
            .endObj()
            .toString()
    }

    /** C0: 十区色码 -> 一条命令(重复 repeat 次)。 */
    fun c0Command(
        colors: List<Int>,
        requestId: String,
        frequencyHz: Long = DEFAULT_FREQ_HZ,
        powerDbm: Int = DEFAULT_POWER_DBM,
        repeat: Int = 1,
        bitUs: Int = BIT_US,
        gapUs: Int = 20000,
    ): String = txBytesCommand(
        buildC0Frame(colors), requestId, frequencyHz, powerDbm, repeat, bitUs, gapUs
    )

    /** 00: 分区命令 -> 一条命令(重复 repeat 次)。 */
    fun zoneCommand(
        m1: Int,
        m2: Int,
        state: Int,
        color: Int,
        requestId: String,
        frequencyHz: Long = DEFAULT_FREQ_HZ,
        powerDbm: Int = DEFAULT_POWER_DBM,
        repeat: Int = 1,
        bitUs: Int = BIT_US,
        gapUs: Int = 20000,
    ): String = txBytesCommand(
        buildZoneFrame(m1, m2, 0xFF, state, color), requestId, frequencyHz, powerDbm,
        repeat, bitUs, gapUs,
    )

    /** 简单命令(GET_INFO / GET_STATUS 之类), 无参数。 */
    fun simpleCommand(cmd: String, requestId: String): String {
        return JsonWriter().obj()
            .put("id", requestId)
            .put("cmd", cmd)
            .endObj()
            .toString()
    }

    /** 查询发射结果。 */
    fun getTxResultCommand(requestId: String, target: String): String {
        val args = JsonWriter().obj().put("request_id", target).endObj().toString()
        return JsonWriter().obj()
            .put("id", requestId)
            .put("cmd", "GET_TX_RESULT")
            .putRaw("args", args)
            .endObj()
            .toString()
    }

    /** Wi-Fi 配网。 */
    fun setWifiConfigCommand(ssid: String, password: String, requestId: String): String {
        val args = JsonWriter().obj()
            .put("ssid", ssid)
            .put("password", password)
            .endObj()
            .toString()
        return JsonWriter().obj()
            .put("id", requestId)
            .put("cmd", "SET_WIFI_CONFIG")
            .putRaw("args", args)
            .endObj()
            .toString()
    }
}
