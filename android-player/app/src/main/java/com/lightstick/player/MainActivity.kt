package com.lightstick.player

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Settings
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.lightstick.player.ui.DeviceScreen
import com.lightstick.player.ui.EditScreen
import com.lightstick.player.ui.LiveScreen
import com.lightstick.player.ui.SequenceScreen
import com.lightstick.player.ui.theme.LightstickTheme

private data class Tab(val labelRes: Int, val icon: ImageVector)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            LightstickTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    AppRoot()
                }
            }
        }
    }
}

@Composable
private fun AppRoot() {
    val vm: AppViewModel = viewModel()
    val params by vm.params.collectAsStateWithLifecycle()
    var tab by remember { mutableIntStateOf(0) }

    val tabs = listOf(
        Tab(R.string.tab_live, Icons.Filled.PlayArrow),
        Tab(R.string.tab_sequence, Icons.Filled.List),
        Tab(R.string.tab_edit, Icons.Filled.Build),
        Tab(R.string.tab_device, Icons.Filled.Settings),
    )

    RequestBluetoothPermissions(vm)

    Scaffold(
        bottomBar = {
            NavigationBar {
                tabs.forEachIndexed { index, item ->
                    NavigationBarItem(
                        selected = tab == index,
                        onClick = { tab = index },
                        icon = { Icon(item.icon, contentDescription = null) },
                        label = { Text(stringResource(item.labelRes)) },
                    )
                }
            }
        },
    ) { insets ->
        Box(Modifier.padding(insets)) {
            when (tab) {
                0 -> LiveScreen(vm = vm, onGoDevice = { tab = 3 })
                1 -> SequenceScreen(vm = vm, params = params)
                2 -> EditScreen()
                else -> DeviceScreen(vm = vm, params = params)
            }
        }
    }
}

/**
 * 蓝牙权限。
 *
 * API 31+ 用 BLUETOOTH_SCAN / BLUETOOTH_CONNECT; API 30 及以下扫描必须给位置权限,
 * 否则扫不到任何设备(而且不会报错, 只是永远空列表)。
 */
@Composable
private fun RequestBluetoothPermissions(vm: AppViewModel) {
    val context = LocalContext.current
    val needed = remember {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }
    }
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { result ->
        val denied = result.filterValues { !it }.keys
        if (denied.isNotEmpty()) vm.log("权限被拒绝: " + denied.joinToString())
    }
    LaunchedEffect(Unit) {
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) launcher.launch(missing.toTypedArray())
    }
}
