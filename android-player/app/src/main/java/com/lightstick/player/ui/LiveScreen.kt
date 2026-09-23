package com.lightstick.player.ui

import androidx.compose.foundation.background
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lightstick.player.AppViewModel
import com.lightstick.player.R
import com.lightstick.player.data.ConnState
import com.lightstick.player.protocol.Protocol

private val Effects = listOf(
    R.string.eff_solid to 0,
    R.string.eff_slow to 1,
    R.string.eff_medium to 2,
    R.string.eff_fast to 3,
)

/** 遥控页: 现场用得最多的一屏, 大按钮、少层级。 */
@Composable
fun LiveScreen(vm: AppViewModel, onGoDevice: () -> Unit) {
    val channels by vm.channels.collectAsStateWithLifecycle()
    val selected by vm.selected.collectAsStateWithLifecycle()
    val connection by vm.connection.collectAsStateWithLifecycle()
    val params by vm.params.collectAsStateWithLifecycle()

    var wheelColor by remember { mutableStateOf(channelColor(channels[selected.minOrNull() ?: 0])) }
    var showAdvanced by remember { mutableStateOf(false) }

    Column(
        Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        // ---- 连接状态条 ----
        StatusBar(connection.state, connection.label, connection.lastError, onGoDevice)

        // ---- 色盘 + 槽位 ----
        SectionCard(stringResource(R.string.live_color)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                ColorWheel(
                    current = wheelColor,
                    onPreview = { color ->
                        wheelColor = color
                        val (r, g, b) = toChannelRgb(color)
                        vm.applyColorLocally(r, g, b)
                    },
                    onCommit = { color ->
                        wheelColor = color
                        val (r, g, b) = toChannelRgb(color)
                        vm.applyColorLocally(r, g, b)
                        vm.pushCurrentState()
                    },
                )
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    HintText(stringResource(R.string.live_drag_hint))
                    Spacer(Modifier.height(8.dp))
                    PaletteRow { code ->
                        wheelColor = paletteColor(code)
                        val (r, g, b) = toChannelRgb(wheelColor)
                        vm.applyColorLocally(r, g, b)
                        vm.pushCurrentState()
                    }
                }
            }
            Spacer(Modifier.height(12.dp))
            SlotRow(
                channels = channels,
                selected = selected,
                onToggle = { vm.toggleSlot(it) },
                onSelectAll = { vm.selectAllSlots() },
            )
        }

        // ---- 效果 ----
        SectionCard(stringResource(R.string.live_effect)) {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                items(Effects.size) { index ->
                    val (labelRes, code) = Effects[index]
                    val active = channels.getOrNull(selected.minOrNull() ?: 0)?.function == code
                    FilterChip(
                        selected = active,
                        onClick = {
                            vm.applyFunctionLocally(code)
                            vm.pushCurrentState()
                        },
                        label = { Text(stringResource(labelRes)) },
                    )
                }
            }
        }

        // ---- 发送 + 全灭 ----
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Button(
                onClick = { vm.pushCurrentState() },
                modifier = Modifier
                    .weight(1f)
                    .height(56.dp),
            ) {
                Text(stringResource(R.string.live_push), fontWeight = FontWeight.Bold)
            }
        }
        Button(
            onClick = { vm.blackout() },
            colors = ButtonDefaults.buttonColors(
                containerColor = MaterialTheme.colorScheme.error,
                contentColor = MaterialTheme.colorScheme.onError,
            ),
            shape = RoundedCornerShape(12.dp),
            modifier = Modifier
                .fillMaxWidth()
                .height(72.dp),
        ) {
            Text(
                text = stringResource(R.string.live_blackout),
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.Bold,
            )
        }
        HintText(stringResource(R.string.live_blackout_hint))

        // ---- 高级参数 ----
        SectionCard(
            title = stringResource(R.string.live_advanced),
            trailing = {
                TextButton(onClick = { showAdvanced = !showAdvanced }) {
                    Text(if (showAdvanced) "收起" else "展开")
                }
            },
        ) {
            HintText(stringResource(R.string.live_advanced_hint))
            if (showAdvanced) {
                Spacer(Modifier.height(8.dp))
                ParamEditor(vm = vm, params = params)
            }
        }
    }
}

@Composable
private fun StatusBar(
    state: ConnState,
    label: String,
    lastError: String?,
    onGoDevice: () -> Unit,
) {
    val dotColor = when (state) {
        ConnState.Connected -> Color(0xFF34C759)
        ConnState.Connecting -> Color(0xFFFFB300)
        ConnState.Disconnected -> MaterialTheme.colorScheme.error
    }
    val text = when (state) {
        ConnState.Connected -> label.ifEmpty { stringResource(R.string.conn_connected) }
        ConnState.Connecting -> stringResource(R.string.conn_connecting)
        ConnState.Disconnected -> lastError ?: stringResource(R.string.conn_disconnected)
    }
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(10.dp)
                .clip(RoundedCornerShape(5.dp))
                .background(dotColor),
        )
        Spacer(Modifier.width(8.dp))
        Text(
            text = text,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(1f),
        )
        TextButton(onClick = onGoDevice) {
            Text(stringResource(R.string.tab_device))
        }
    }
}

/** 射频参数编辑器, 遥控页和设备页共用。 */
@Composable
fun ParamEditor(vm: AppViewModel, params: com.lightstick.player.data.TxParams) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                stringResource(R.string.param_mode),
                style = MaterialTheme.typography.labelMedium,
                modifier = Modifier.width(120.dp),
            )
            LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                items(3) { index ->
                    val mode = com.lightstick.player.data.Mode.entries[index]
                    val label = when (mode) {
                        com.lightstick.player.data.Mode.D8 -> stringResource(R.string.mode_d8)
                        com.lightstick.player.data.Mode.C0 -> stringResource(R.string.mode_c0)
                        com.lightstick.player.data.Mode.ZONE -> stringResource(R.string.mode_zone)
                    }
                    FilterChip(
                        selected = params.mode == mode,
                        onClick = { vm.setMode(mode) },
                        label = { Text(label, style = MaterialTheme.typography.labelSmall) },
                    )
                }
            }
        }
        NumberRow(
            label = stringResource(R.string.param_burst),
            value = params.burstFrames.toString(),
            options = listOf("1", "2", "3", "6"),
            onPick = { vm.updateParams { p -> p.copy(burstFrames = it.toInt()) } },
        )
        NumberRow(
            label = stringResource(R.string.param_bit_us),
            value = params.bitUs.toString(),
            options = listOf("250", "225", "200"),
            onPick = { vm.updateParams { p -> p.copy(bitUs = it.toInt()) } },
        )
        HintText(stringResource(R.string.param_bit_us_hint))
        NumberRow(
            label = stringResource(R.string.param_repeat),
            value = params.repeat.toString(),
            options = listOf("1", "2", "3", "6"),
            onPick = { vm.updateParams { p -> p.copy(repeat = it.toInt()) } },
        )
        NumberRow(
            label = stringResource(R.string.param_power),
            value = params.powerDbm.toString(),
            options = listOf("-30", "-20", "-10", "0"),
            onPick = { vm.updateParams { p -> p.copy(powerDbm = it.toInt()) } },
        )
        Row(verticalAlignment = Alignment.CenterVertically) {
            FilterChip(
                selected = params.throttle,
                onClick = { vm.updateParams { p -> p.copy(throttle = !p.throttle) } },
                label = { Text(stringResource(R.string.param_throttle)) },
            )
            Spacer(Modifier.width(6.dp))
            FilterChip(
                selected = params.dropLate,
                onClick = { vm.updateParams { p -> p.copy(dropLate = !p.dropLate) } },
                label = { Text(stringResource(R.string.param_drop_late)) },
            )
        }
    }
}

@Composable
private fun NumberRow(
    label: String,
    value: String,
    options: List<String>,
    onPick: (String) -> Unit,
) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelMedium,
            modifier = Modifier.width(120.dp),
        )
        LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            items(options.size) { index ->
                val option = options[index]
                FilterChip(
                    selected = value == option,
                    onClick = { onPick(option) },
                    label = {
                        Text(option, style = MaterialTheme.typography.labelSmall)
                    },
                )
            }
        }
    }
}
