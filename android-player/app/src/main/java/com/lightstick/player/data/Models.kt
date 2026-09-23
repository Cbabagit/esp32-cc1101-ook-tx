package com.lightstick.player.data

/** 一个通道(slot)的状态。r/g/b 是 0..15 的 4bit 分量, function 是 0..3 的效果码。 */
data class Channel(
    val function: Int = 0,
    val r: Int = 0,
    val g: Int = 0,
    val b: Int = 0,
) {
    val isEmpty: Boolean get() = r == 0 && g == 0 && b == 0
}

/** CSV 里的一帧。timeMs 是相对起点的毫秒时间戳。 */
data class Frame(
    val timeMs: Long,
    val id: Int,
    val type: String,
    val channels: List<Channel>,
)

/** 一条完整序列。 */
data class Sequence(
    val name: String,
    val frames: List<Frame>,
) {
    val durationMs: Long get() = frames.lastOrNull()?.timeMs ?: 0L
    val channelCount: Int get() = frames.firstOrNull()?.channels?.size ?: 0
}

/** 发射模式。对应 protocol.py 里的三种帧编码。 */
enum class Mode { D8, C0, ZONE }

/** 连接方式。USB 串口在界面上保留入口, 实现在后面阶段补。 */
enum class TransportKind { USB, UDP, HTTP, BLE }

/** 一组发射参数。默认值取自协议实测结论。 */
data class TxParams(
    val mode: Mode = Mode.D8,
    val burstFrames: Int = 6,
    val bitUs: Int = 250,
    val gapUs: Int = 850,
    val repeat: Int = 1,
    val frequencyHz: Long = 433_920_000L,
    val powerDbm: Int = -20,
    val throttle: Boolean = true,
    val dropLate: Boolean = true,
)
