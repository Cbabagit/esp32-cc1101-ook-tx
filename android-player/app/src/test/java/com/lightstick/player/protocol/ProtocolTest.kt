package com.lightstick.player.protocol

import com.lightstick.player.data.Channel
import com.lightstick.player.protocol.Protocol.toHex
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 协议层金标测试。
 *
 * 下面所有期望值都来自 Python 端 lightstick-player/protocol.py 的实际输出
 * (用 _gen_golden.py 生成)。两端只要有一边改歪, 这里就会红。
 */
class ProtocolTest {

    private val red = Channel(function = 0, r = 15, g = 0, b = 0)
    private val redSlots = IntArray(9) { 0x0F00 }

    // ---- 校验和 ----

    @Test
    fun checksumMatchesPython() {
        assertEquals(0x94, Protocol.checksum(byteArrayOf(0x00, 0xFF.toByte(), 0xFF.toByte(), 0xFF.toByte(), 0x01, 0x00)))
        assertEquals(0x56, Protocol.checksum(byteArrayOf(0xC0.toByte(), 0, 0, 0, 0, 0)))
    }

    // ---- 帧构造 (hex 与 Python 逐字节一致) ----

    @Test
    fun framesMatchPython() {
        assertEquals("00FFFFFF010094", Protocol.buildZoneFrame(0xFF, 0xFF, 0xFF, 0x01, 0x00).toHex())
        assertEquals("C00123456789AF", Protocol.buildC0Frame(listOf(0, 1, 2, 3, 4, 5, 6, 7, 8, 9)).toHex())
        assertEquals("A60743", Protocol.buildA6Frame(0x07).toHex())
        assertEquals("DAFFFFFF01006E", Protocol.buildDaFrame(0xFF, 0xFF).toHex())
        assertEquals(
            "D83C003C003C003C003C003C003C003C003C00028C",
            Protocol.buildD8Frame(redSlots, 2).toHex(),
        )
        assertEquals(
            "D848D059E16AF2000000000000000000000000001C",
            Protocol.buildD8Frame(intArrayOf(0x1234, 0x5678, 0x9ABC, 0, 0, 0, 0, 0, 0), 0).toHex(),
        )
    }

    // ---- 空口编码 ----

    @Test
    fun symbolCountsMatchPython() {
        val c0 = Protocol.buildC0Frame(List(10) { 0 })
        val d8 = Protocol.buildD8Frame(redSlots, 2)
        assertEquals(185, Protocol.frameToBits(c0).length)
        assertEquals(521, Protocol.frameToBits(d8).length)
        assertEquals(46250L, Protocol.frameDurationUs(c0))
        assertEquals(130250L, Protocol.frameDurationUs(d8))
        assertEquals(104200L, Protocol.frameDurationUs(d8, 200))
    }

    @Test
    fun d8BurstsMatchPython() {
        val one = Protocol.buildD8Transaction(redSlots, 250, 1)
        val three = Protocol.buildD8Transaction(redSlots, 250, 3)
        val six = Protocol.buildD8Transaction(redSlots, 250, 6)

        assertEquals(340, one.size)
        assertEquals(130250L, one.sum().toLong())
        assertEquals(1020, three.size)
        assertEquals(392450L, three.sum().toLong())
        assertEquals(2040, six.size)
        assertEquals(785750L, six.sum().toLong())
        assertEquals(2000, six[0])   // 起始是 8 个前导 1 = 8 x 250us
    }

    @Test
    fun timingHelpersMatchPython() {
        assertEquals(785750L, Protocol.d8AirTimeUs(6))
        assertEquals(786, Protocol.modeIntervalMs("d8", 6))
        assertEquals(393, Protocol.modeIntervalMs("d8", 3))
        assertEquals(380, Protocol.modeIntervalMs("zone"))
        assertEquals(200, Protocol.MIN_BIT_US)
    }

    // ---- 槽字与调色板 ----

    @Test
    fun slotWordMatchesPython() {
        assertEquals(3840, Protocol.slotWord(0, 15, 0, 0))
        assertEquals(4111, Protocol.slotWord(1, 0, 0, 15))
        assertEquals(0x0F00, Protocol.slotWord(0, 15, 0, 0))
    }

    @Test
    fun paletteMatchesPython() {
        assertEquals(13, Protocol.nearestPalette(0, 0, 0))
        assertEquals(0, Protocol.nearestPalette(15, 0, 0))
        assertEquals(1, Protocol.nearestPalette(0, 15, 0))
        assertEquals(2, Protocol.nearestPalette(0, 0, 15))
        assertEquals(4, Protocol.nearestPalette(15, 15, 15))
        assertEquals(8, Protocol.nearestPalette(8, 8, 8))
    }

    // ---- 命令 JSON (与 Python json.dumps 逐字节一致) ----

    @Test
    fun txD8CommandMatchesPython() {
        val expected = """{"id":"7","cmd":"TX_D8","args":{"slots":[3840,3840,3840,3840,3840,3840,3840,3840,3840],"burst":1,"bit_us":250,"gap_us":850,"frequency_hz":433920000,"power_dbm":-20}}"""
        val actual = Protocol.txD8Command(
            List(9) { red }, requestId = "7", frequencyHz = 433_920_000L, powerDbm = -20,
            burstFrames = 1, bitUs = 250, gapUs = 850,
        )
        assertEquals(expected, actual)
    }

    @Test
    fun txBytesCommandMatchesPython() {
        val expected = """{"id":"9","cmd":"TX_BYTES","args":{"data_hex":"C0000000000056","repeat":1,"bit_us":250,"gap_us":20000,"frequency_hz":433920000,"power_dbm":-20}}"""
        val actual = Protocol.txBytesCommand(
            Protocol.buildC0Frame(List(10) { 0 }), requestId = "9",
            frequencyHz = 433_920_000L, powerDbm = -20, repeat = 1, bitUs = 250, gapUs = 20000,
        )
        assertEquals(expected, actual)
    }

    @Test
    fun jsonEscapingIsSafe() {
        // 输入 a"b 应转义成 a\"b (中间多一个反斜杠)
        val quote = 34.toChar()
        val backslash = 92.toChar()
        assertEquals(
            "a" + backslash + quote + "b",
            JsonWriter.escape("a" + quote + "b"),
        )
        assertEquals("{}", JsonWriter().obj().endObj().toString())
    }

    // ---- 黑场判定 ----

    @Test
    fun blackoutDetection() {
        assertEquals(true, Protocol.isBlackout("blackout", listOf(red)))
        assertEquals(true, Protocol.isBlackout("color", listOf(Channel())))
        assertEquals(false, Protocol.isBlackout("color", listOf(red)))
        assertEquals(false, Protocol.isBlackout("", listOf(red)))
    }
}
