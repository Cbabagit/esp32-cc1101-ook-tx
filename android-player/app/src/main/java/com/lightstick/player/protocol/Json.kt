package com.lightstick.player.protocol

/**
 * 极简 JSON 写入器。
 *
 * 为什么不直接用 org.json: 单元测试跑在纯 JVM 上, org.json 属于 Android 平台类,
 * 在 JVM 单测里会抛 "not mocked"; 而且协议命令的 JSON 要和 Python 端的
 * json.dumps(separators=(",", ":")) 逐字节一致, 自己拼更可控。
 *
 * 为了避开反斜杠转义地狱, 所有特殊字符都用码点常量表示。
 */
class JsonWriter {
    private val sb = StringBuilder(256)
    private var first = true

    fun obj(): JsonWriter = apply { sb.append(123.toChar()); first = true }
    fun endObj(): JsonWriter = apply { sb.append(125.toChar()); first = false }
    fun arr(): JsonWriter = apply { sb.append(91.toChar()); first = true }
    fun endArr(): JsonWriter = apply { sb.append(93.toChar()); first = false }

    private fun key(name: String) {
        if (!first) sb.append(44.toChar())
        first = false
        sb.append(QUOTE).append(escape(name)).append(QUOTE).append(58.toChar())
    }

    fun put(name: String, value: String): JsonWriter = apply {
        key(name)
        sb.append(QUOTE).append(escape(value)).append(QUOTE)
    }

    /** 原样嵌入一段已经是 JSON 的文本(嵌套对象/数组), 不要当字符串转义。 */
    fun putRaw(name: String, rawJson: String): JsonWriter = apply {
        key(name)
        sb.append(rawJson)
    }

    fun put(name: String, value: Int): JsonWriter = apply { key(name); sb.append(value) }

    fun put(name: String, value: Long): JsonWriter = apply { key(name); sb.append(value) }

    fun put(name: String, value: Boolean): JsonWriter = apply {
        key(name)
        sb.append(if (value) "true" else "false")
    }

    fun putIntArray(name: String, values: IntArray): JsonWriter = apply {
        key(name)
        sb.append(91.toChar())
        values.forEachIndexed { index, value ->
            if (index > 0) sb.append(44.toChar())
            sb.append(value)
        }
        sb.append(93.toChar())
    }

    fun putStringArray(name: String, values: List<String>): JsonWriter = apply {
        key(name)
        sb.append(91.toChar())
        values.forEachIndexed { index, value ->
            if (index > 0) sb.append(44.toChar())
            sb.append(QUOTE).append(escape(value)).append(QUOTE)
        }
        sb.append(93.toChar())
    }

    override fun toString(): String = sb.toString()

    companion object {
        private const val QUOTE = 34.toChar()
        private const val BACKSLASH = 92.toChar()
        private const val LF = 10
        private const val CR = 13
        private const val TAB = 9

        fun escape(text: String): String {
            val out = StringBuilder(text.length + 8)
            for (ch in text) {
                when (ch.code) {
                    34 -> out.append(BACKSLASH).append(QUOTE)
                    92 -> out.append(BACKSLASH).append(BACKSLASH)
                    LF -> out.append(BACKSLASH).append(110.toChar())
                    CR -> out.append(BACKSLASH).append(114.toChar())
                    TAB -> out.append(BACKSLASH).append(116.toChar())
                    else -> if (ch.code < 32) {
                        out.append(BACKSLASH).append(117.toChar())
                        out.append(String.format("%04x", ch.code))
                    } else {
                        out.append(ch)
                    }
                }
            }
            return out.toString()
        }
    }
}
