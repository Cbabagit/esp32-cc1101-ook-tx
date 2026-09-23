package com.lightstick.player.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lightstick.player.AppViewModel
import com.lightstick.player.R
import com.lightstick.player.data.ConnState
import com.lightstick.player.data.TransportKind
import com.lightstick.player.protocol.Protocol

private const val WIFI_HELP = """板子会把这组凭据存进 NVS, 之后开机自动连。

现场换网络时不用带电脑, 在这里改就行。

注意: 板子只支持 2.4GHz Wi-Fi。"""

private const val TERMINAL_HELP = """直接发一行 JSON 给板子, 原样显示它的回复。

例: GET_INFO / GET_STATUS / GET_NETWORK_STATUS / GET_COMMANDS
(GET_COMMANDS 会列出固件支持的所有命令)

排查问题时先发 GET_INFO 看固件版本和 CC1101 状态。"""

/** 设备页: 连接、配网、射频参数、调试终端、日志。 */
@Composable
fun DeviceScreen(vm: AppViewModel, params: com.lightstick.player.data.TxParams) {
    val connection by vm.connection.collectAsStateWithLifecycle()
    val device by vm.device.collectAsStateWithLifecycle()
    val logs by vm.logs.collectAsStateWithLifecycle()
    val lastReply by vm.lastReply.collectAsStateWithLifecycle()
    val terminal by vm.terminalInput.collectAsStateWithLifecycle()

    var ssid by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }

    Column(
        Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        SectionCard(stringResource(R.string.conn_section)) {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                items(4) { index ->
                    val kind = TransportKind.entries[index]
                    val label = when (kind) {
                        TransportKind.USB -> stringResource(R.string.kind_usb)
                        TransportKind.UDP -> stringResource(R.string.kind_udp)
                        TransportKind.HTTP -> stringResource(R.string.kind_http)
                        TransportKind.BLE -> stringResource(R.string.kind_ble)
                    }
                    FilterChip(
                        selected = connection.kind == kind,
                        onClick = { vm.setTransportKind(kind) },
                        label = { Text(label, style = MaterialTheme.typography.labelSmall) },
                    )
                }
            }
            Spacer(Modifier.height(8.dp))
            val hint = when (connection.kind) {
                TransportKind.UDP -> stringResource(R.string.conn_address_udp)
                TransportKind.BLE -> stringResource(R.string.conn_address_ble)
                TransportKind.HTTP -> stringResource(R.string.conn_address_http)
                TransportKind.USB -> stringResource(R.string.conn_address_usb)
            }
            OutlinedTextField(
                value = connection.address,
                onValueChange = { vm.setAddress(it) },
                label = { Text(hint) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                val connected = connection.state == ConnState.Connected
                Button(
                    onClick = { if (connected) vm.disconnect() else vm.connect() },
                    enabled = connection.state != ConnState.Connecting,
                ) {
                    Text(
                        when (connection.state) {
                            ConnState.Connected -> stringResource(R.string.conn_disconnect)
                            ConnState.Connecting -> stringResource(R.string.conn_connecting)
                            ConnState.Disconnected -> stringResource(R.string.conn_connect)
                        },
                    )
                }
                OutlinedButton(
                    onClick = { vm.send(Protocol.simpleCommand("GET_INFO", vm.nextId())) },
                    enabled = connected,
                ) { Text(stringResource(R.string.dev_refresh)) }
            }
            connection.lastError?.let {
                Spacer(Modifier.height(6.dp))
                Text(
                    text = it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }

        SectionCard(stringResource(R.string.dev_info)) {
            val info = device
            if (info == null) {
                Text(
                    stringResource(R.string.dev_info_empty),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                InfoRow(stringResource(R.string.dev_firmware), info.firmwareVersion)
                InfoRow(stringResource(R.string.dev_chip), info.chip)
                InfoRow(
                    stringResource(R.string.dev_radio),
                    if (info.cc1101Found) "OK" else "not found",
                )
                InfoRow(stringResource(R.string.dev_memory), info.freeHeap.toString() + " B")
                InfoRow(
                    stringResource(R.string.dev_wifi),
                    if (info.wifiConnected) {
                        info.wifiSsid + " · " + info.ip
                    } else {
                        "disconnected · " + info.wifiSsid
                    },
                )
            }
        }

        SectionCard(
            title = stringResource(R.string.dev_wifi_config),
            trailing = {
                HelpButton(
                    title = stringResource(R.string.dev_wifi_config),
                    body = WIFI_HELP,
                )
            },
        ) {
            OutlinedTextField(
                value = ssid,
                onValueChange = { ssid = it },
                label = { Text(stringResource(R.string.dev_ssid)) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(
                value = password,
                onValueChange = { password = it },
                label = { Text(stringResource(R.string.dev_password)) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = {
                    vm.send(Protocol.setWifiConfigCommand(ssid, password, vm.nextId()))
                    vm.send(Protocol.simpleCommand("CONNECT_WIFI", vm.nextId()))
                },
                enabled = ssid.isNotBlank(),
            ) { Text(stringResource(R.string.dev_send_wifi)) }
        }

        SectionCard(stringResource(R.string.dev_radio_params)) {
            ParamEditor(vm = vm, params = params)
        }

        SectionCard(
            title = stringResource(R.string.dev_terminal),
            trailing = {
                HelpButton(
                    title = stringResource(R.string.dev_terminal),
                    body = TERMINAL_HELP,
                )
            },
        ) {
            HintText(stringResource(R.string.dev_terminal_hint))
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(
                value = terminal,
                onValueChange = { vm.setTerminalInput(it) },
                label = { Text("JSON") },
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(6.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Button(onClick = { vm.sendTerminal() }) {
                    Text(stringResource(R.string.dev_send))
                }
                OutlinedButton(onClick = { vm.setTerminalInput("GET_COMMANDS") }) {
                    Text("GET_COMMANDS")
                }
            }
            if (lastReply.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    text = lastReply,
                    fontFamily = FontFamily.Monospace,
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        LogPanel(logs = logs, onClear = { vm.clearLogs() })
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(Modifier.padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelMedium,
            modifier = Modifier.width(110.dp),
        )
        Text(text = value.ifEmpty { "-" }, style = MaterialTheme.typography.bodySmall)
    }
}
