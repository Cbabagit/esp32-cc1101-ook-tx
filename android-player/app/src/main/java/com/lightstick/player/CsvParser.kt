package com.lightstick.player

import com.lightstick.player.data.Channel
import com.lightstick.player.data.Frame
import com.lightstick.player.data.Sequence

/**
 * CSV 解析, 与 PC 端 lightstick-player/csv_loader.py 完全一致。
 *
 * 列名: frame_time_ms, frame_id, frame_type, marker,
 *       ch{N}_function, ch{N}_red, ch{N}_green, ch{N}_blue  (N = 0..9)
 * 取值: function 0..3, 每个颜色分量 0..15。
 *
 * 两端的 CSV 可以直接互相交换文件。
 */
object CsvParser {

    fun parse(text: String, name: String): Sequence {
        val lines = text.split(10.toChar(), 13.toChar())
            .filter { it.isNotBlank() }
        require(lines.isNotEmpty()) { "CSV 是空的" }

        val header = splitRow(lines[0])
        val channelCount = detectChannelCount(header)
        require(channelCount > 0) { "CSV 缺少 ch0_function 列" }

        val columnIndex = HashMap<String, Int>()
        header.forEachIndexed { index, title -> columnIndex[title.trim()] = index }

        val frames = ArrayList<Frame>(lines.size - 1)
        for (lineIndex in 1 until lines.size) {
            val row = splitRow(lines[lineIndex])
            val channels = ArrayList<Channel>(channelCount)
            for (i in 0 until channelCount) {
                channels.add(
                    Channel(
                        function = clamp4(value(row, columnIndex, "ch" + i + "_function"), 0x3),
                        r = clamp4(value(row, columnIndex, "ch" + i + "_red"), 0xF),
                        g = clamp4(value(row, columnIndex, "ch" + i + "_green"), 0xF),
                        b = clamp4(value(row, columnIndex, "ch" + i + "_blue"), 0xF),
                    )
                )
            }
            frames.add(
                Frame(
                    timeMs = value(row, columnIndex, "frame_time_ms").toDoubleOrNull()?.toLong() ?: 0L,
                    id = value(row, columnIndex, "frame_id").toDoubleOrNull()?.toInt() ?: lineIndex - 1,
                    type = value(row, columnIndex, "frame_type"),
                    channels = channels,
                )
            )
        }
        frames.sortBy { it.timeMs }
        return Sequence(name = name, frames = frames)
    }

    private fun detectChannelCount(header: List<String>): Int {
        var count = 0
        while (header.any { it.trim() == "ch" + count + "_function" }) count++
        return count
    }

    private fun value(row: List<String>, index: Map<String, Int>, key: String): String {
        val position = index[key] ?: return ""
        return if (position < row.size) row[position].trim() else ""
    }

    private fun clamp4(raw: String, mask: Int): Int {
        val parsed = raw.toDoubleOrNull()?.toInt() ?: 0
        return parsed and mask
    }

    /** 按逗号切分, 支持双引号包裹的字段。 */
    private fun splitRow(line: String): List<String> {
        val out = ArrayList<String>(24)
        val sb = StringBuilder()
        var inQuotes = false
        var index = 0
        while (index < line.length) {
            val ch = line[index]
            when {
                ch == 34.toChar() -> {
                    if (inQuotes && index + 1 < line.length && line[index + 1] == 34.toChar()) {
                        sb.append(34.toChar())
                        index++
                    } else {
                        inQuotes = !inQuotes
                    }
                }
                ch == 44.toChar() && !inQuotes -> {
                    out.add(sb.toString())
                    sb.setLength(0)
                }
                else -> sb.append(ch)
            }
            index++
        }
        out.add(sb.toString())
        return out
    }
}
