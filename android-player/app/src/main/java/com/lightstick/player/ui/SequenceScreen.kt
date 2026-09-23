package com.lightstick.player.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lightstick.player.AppViewModel
import com.lightstick.player.R

/** 序列页: 播放 CSV。格式与 PC 端一致, 文件可以互相交换。 */
@Composable
fun SequenceScreen(vm: AppViewModel, params: com.lightstick.player.data.TxParams) {
    val sequence by vm.sequence.collectAsStateWithLifecycle()
    val playback by vm.playback.collectAsStateWithLifecycle()

    val picker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument(),
    ) { uri -> vm.loadSequence(uri) }

    Column(
        Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        SectionCard(stringResource(R.string.seq_section)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(onClick = { picker.launch(arrayOf("*/*")) }) {
                    Text(stringResource(R.string.seq_pick))
                }
                Spacer(Modifier.width(10.dp))
                Text(
                    text = sequence?.let { it.name + " · " + it.frames.size + " 帧" }
                        ?: stringResource(R.string.seq_none),
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.weight(1f),
                )
            }
            HintText(stringResource(R.string.seq_export_hint))

            if (sequence != null) {
                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = { vm.playSequence() },
                        enabled = !playback.playing,
                    ) { Text(stringResource(R.string.seq_play)) }
                    OutlinedButton(
                        onClick = { vm.pauseSequence() },
                        enabled = playback.playing,
                    ) { Text(stringResource(R.string.seq_pause)) }
                    OutlinedButton(onClick = { vm.stopSequence() }) {
                        Text(stringResource(R.string.seq_stop))
                    }
                }

                Spacer(Modifier.height(12.dp))
                LinearProgressIndicator(
                    progress = { playback.progress },
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    text = stringResource(
                        R.string.seq_stat,
                        playback.framesSent,
                        playback.framesDropped,
                        playback.intervalMs,
                    ),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )

                Spacer(Modifier.height(12.dp))
                FramePreview(vm)
            }
        }

        SectionCard(stringResource(R.string.param_throttle)) {
            ParamEditor(vm = vm, params = params)
        }
    }
}

/** 用 9 个色块显示当前播放到的帧。 */
@Composable
private fun FramePreview(vm: AppViewModel) {
    val channels by vm.channels.collectAsStateWithLifecycle()
    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        channels.forEach { channel ->
            Box(
                Modifier
                    .size(28.dp)
                    .clip(RoundedCornerShape(6.dp))
                    .background(channelColor(channel)),
            )
        }
    }
}
