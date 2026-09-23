package com.lightstick.player.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.lightstick.player.R
import com.lightstick.player.data.Channel
import com.lightstick.player.protocol.Protocol
import com.lightstick.player.ui.theme.Palette16
import kotlin.math.atan2
import kotlin.math.hypot

/** 带标题的卡片, 全 App 统一用它分组。 */
@Composable
fun SectionCard(
    title: String,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = title,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.weight(1f),
                )
                trailing?.invoke()
            }
            Spacer(Modifier.height(8.dp))
            content()
        }
    }
}

/** 复杂操作的说明弹窗。 */
@Composable
fun HelpDialog(title: String, body: String, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Text(body, style = MaterialTheme.typography.bodyMedium)
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text(stringResource(R.string.common_close)) }
        },
    )
}

/** 小字号的"?"按钮, 点开显示说明。 */
@Composable
fun HelpButton(title: String, body: String) {
    var open by remember { mutableStateOf(false) }
    TextButton(onClick = { open = true }) {
        Text(stringResource(R.string.common_help), style = MaterialTheme.typography.labelMedium)
    }
    if (open) HelpDialog(title, body, onDismiss = { open = false })
}

private val HueColors = List(37) { index ->
    Color.hsv((index * 360f / 36f) % 360f, 1f, 1f)
}

/**
 * HSV 色盘: 角度是色相, 半径是饱和度, 明度固定为 1。
 *
 * 拖动过程中只回调 onPreview(改本地预览), 松手才回调 onCommit(真正发命令),
 * 否则每移动一下就发一条命令会把链路打满。
 */
@Composable
fun ColorWheel(
    current: Color,
    onPreview: (Color) -> Unit,
    onCommit: (Color) -> Unit,
    modifier: Modifier = Modifier,
) {
    var indicator by remember { mutableStateOf<Offset?>(null) }
    fun toColor(offset: Offset, radius: Float): Color {
        val dx = offset.x - radius
        val dy = offset.y - radius
        val distance = hypot(dx, dy)
        val hue = (Math.toDegrees(atan2(dy.toDouble(), dx.toDouble())).toFloat() + 360f) % 360f
        val saturation = (distance / radius).coerceIn(0f, 1f)
        return Color.hsv(hue, saturation, 1f)
    }
    // 先在组合作用域里取出来, 绘制 lambda 里不能调 @Composable
    val outlineColor = MaterialTheme.colorScheme.outline
    Box(modifier.size(220.dp), contentAlignment = Alignment.Center) {
        Canvas(
            modifier = Modifier
                .size(220.dp)
                .pointerInput(Unit) {
                    awaitPointerEventScope {
                        while (true) {
                            val down = awaitFirstDown()
                            val radius = minOf(size.width, size.height) / 2f
                            indicator = down.position
                            onPreview(toColor(down.position, radius))
                            down.consume()
                            var pressed = true
                            while (pressed) {
                                val event = awaitPointerEvent()
                                val change = event.changes.firstOrNull()
                                if (change == null || !change.pressed) {
                                    pressed = false
                                } else {
                                    indicator = change.position
                                    onPreview(toColor(change.position, radius))
                                    change.consume()
                                }
                            }
                            onCommit(toColor(indicator ?: Offset(radius, radius), radius))
                        }
                    }
                },
        ) {
            val radius = size.minDimension / 2f
            drawCircle(brush = Brush.sweepGradient(HueColors), radius = radius)
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(Color.White, Color.White.copy(alpha = 0f)),
                    radius = radius,
                ),
                radius = radius,
            )
            drawCircle(
                color = outlineColor,
                radius = radius,
                style = Stroke(width = 2f),
            )
            indicator?.let {
                drawCircle(
                    color = Color.White,
                    radius = 9f,
                    center = it,
                    style = Stroke(width = 3f),
                )
                drawCircle(color = current, radius = 6f, center = it)
            }
        }
    }
}

/** 槽位选择。可多选, 选中的会一起改颜色。 */
@Composable
fun SlotRow(
    channels: List<Channel>,
    selected: Set<Int>,
    onToggle: (Int) -> Unit,
    onSelectAll: () -> Unit,
) {
    Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                text = stringResource(R.string.live_slots),
                style = MaterialTheme.typography.labelMedium,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = onSelectAll) {
                Text(stringResource(R.string.live_select_all))
            }
        }
        LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            items(channels.size) { index ->
                val channel = channels[index]
                val isSelected = selected.contains(index)
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.clickable { onToggle(index) },
                ) {
                    Box(
                        modifier = Modifier
                            .size(40.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .background(
                                Color(
                                    0xFF000000.toInt() or
                                        (channel.r * 17 shl 16) or
                                        (channel.g * 17 shl 8) or
                                        (channel.b * 17),
                                ),
                            )
                            .border(
                                width = if (isSelected) 3.dp else 1.dp,
                                color = if (isSelected) {
                                    MaterialTheme.colorScheme.primary
                                } else {
                                    MaterialTheme.colorScheme.outline
                                },
                                shape = RoundedCornerShape(8.dp),
                            ),
                    )
                    Text(
                        text = (index + 1).toString(),
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
            }
        }
    }
}

/** 16 色快选。 */
@Composable
fun PaletteRow(onPick: (Int) -> Unit) {
    LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        items(Palette16.size) { index ->
            Box(
                modifier = Modifier
                    .size(34.dp)
                    .clip(CircleShape)
                    .background(Palette16[index])
                    .border(1.dp, MaterialTheme.colorScheme.outline, CircleShape)
                    .clickable { onPick(index) },
            )
        }
    }
}

/** 日志面板。等宽字体, 自动滚到最新。 */
@Composable
fun LogPanel(logs: List<String>, onClear: () -> Unit, modifier: Modifier = Modifier) {
    SectionCard(
        title = stringResource(R.string.dev_logs),
        modifier = modifier,
        trailing = {
            TextButton(onClick = onClear) { Text(stringResource(R.string.dev_clear)) }
        },
    ) {
        val scroll = rememberScrollState()
        Column(
            Modifier
                .fillMaxWidth()
                .height(140.dp)
                .verticalScroll(scroll),
        ) {
            logs.takeLast(120).forEach { line ->
                Text(
                    text = line,
                    fontFamily = FontFamily.Monospace,
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/** 一行小字说明, 用来解释不那么直观的参数。 */
@Composable
fun HintText(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 2.dp, start = 2.dp),
    )
}

/** 把 0..15 的三分量转成 Compose Color。 */
fun channelColor(channel: Channel): Color {
    return Color(
        0xFF000000.toInt() or
            (channel.r * 17 shl 16) or
            (channel.g * 17 shl 8) or
            (channel.b * 17),
    )
}

/** Compose Color -> 0..15 的三分量。 */
fun toChannelRgb(color: Color): Triple<Int, Int, Int> {
    val r = (color.red * 15f + 0.5f).toInt().coerceIn(0, 15)
    val g = (color.green * 15f + 0.5f).toInt().coerceIn(0, 15)
    val b = (color.blue * 15f + 0.5f).toInt().coerceIn(0, 15)
    return Triple(r, g, b)
}

/** 便捷: 调色板色码 -> Channel 颜色。 */
fun paletteColor(code: Int): Color = Palette16[code.coerceIn(0, 15)]

/** 供调试终端等处显示协议常量。 */
fun slotCount(): Int = Protocol.SLOT_WORDS
