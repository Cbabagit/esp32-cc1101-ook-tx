package com.lightstick.player.transport

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Build
import android.os.ParcelUuid
import com.lightstick.player.data.TransportKind
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

/**
 * BLE (GATT)。
 *
 * 对应固件 firmware/src/main.cpp 里的 beginBle():
 *  服务 8f7a0001-…, 命令特征 …0002(写), 响应特征 …0003(读+通知)。
 * 命令是换行分隔的 JSON, 和设备名/串口完全一致; 响应按 180 字节分片通知回来,
 * 这里用换行做帧界拼回去。
 *
 * Android GATT 的三条硬约束, 这个类就是围着它们写的:
 *  1. 一次只能有一个未完成的操作 —— 所以 MTU 协商和发现服务必须串行,
 *     写操作也必须排队, 靠 onCharacteristicWrite 回调驱动下一笔。
 *  2. 回调都在 binder 线程 —— 状态用 @Volatile, 写队列用 lock 保护。
 *  3. 回调可能永远不来 —— 所以有写超时看门狗和 MTU 兜底定时器。
 */
@SuppressLint("MissingPermission")
class BleTransport(
    private val context: Context,
    private val scope: CoroutineScope,
    private val deviceName: String? = null,
    private val deviceAddress: String? = null,
) : Transport() {

    override val kind = TransportKind.BLE

    private val serviceUuid: UUID = UUID.fromString(TransportDefaults.BLE_SERVICE_UUID)
    private val commandUuid: UUID = UUID.fromString(TransportDefaults.BLE_COMMAND_UUID)
    private val responseUuid: UUID = UUID.fromString(TransportDefaults.BLE_RESPONSE_UUID)

    private var gatt: BluetoothGatt? = null
    private var commandChar: BluetoothGattCharacteristic? = null
    private var label: String = deviceAddress ?: deviceName ?: TransportDefaults.BLE_DEVICE_NAME

    @Volatile
    private var connected = false

    @Volatile
    private var closed = false

    @Volatile
    private var mtu = 23

    private val ready = CompletableDeferred<Unit>()
    private val notifyBuffer = StringBuilder()
    private val servicesRequested = AtomicBoolean(false)

    /** 写队列的保护锁。pump() 会被 pump 协程和 GATT 回调线程同时调用, 必须串行化。 */
    private val lock = Any()
    private val writeQueue = ArrayDeque<ByteArray>()
    private var writing = false
    private var lastWriteAt = 0L

    /** 最近一次待发命令只保留一条(和 UDP/串口一致), 避免断线时积压。 */
    @Volatile
    private var latest: ByteArray? = null

    private var pumpJob: Job? = null
    private var watchdogJob: Job? = null
    private var mtuFallbackJob: Job? = null

    override fun describe(): String = "ble " + label

    override fun isConnected(): Boolean = connected && (gatt != null)

    override suspend fun open() = withContext(Dispatchers.IO) {
        val manager = context.getSystemService(BluetoothManager::class.java)
        val adapter = manager?.adapter
            ?: throw IllegalStateException("这台设备没有蓝牙适配器")
        if (!adapter.isEnabled) {
            throw IllegalStateException("蓝牙没有打开, 请先在系统设置里开启蓝牙")
        }

        val device = if (!deviceAddress.isNullOrBlank()) {
            label = deviceAddress
            adapter.getRemoteDevice(deviceAddress)
        } else {
            val wanted = deviceName?.takeIf { it.isNotBlank() } ?: TransportDefaults.BLE_DEVICE_NAME
            log("BLE: 扫描 " + wanted + " …")
            scanForDevice(adapter, wanted)
        }
        label = device.name ?: device.address
        log("BLE: 连接 " + label + " …")

        val g = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
            ?: throw IllegalStateException("connectGatt 返回空, 无法建立连接")
        gatt = g
        try {
            // MTU + 发现服务 + 订阅通知都要走完, 20 秒是给慢机型留的余量
            withTimeout(20_000) { ready.await() }
        } catch (e: Exception) {
            close()
            throw IllegalStateException("BLE 连接超时或失败: " + (e.message ?: "未知原因"))
        }
        log("BLE: 就绪 (MTU=" + mtu + "), 可以发命令了")
        startPump()
        emitState(true)
    }

    private suspend fun scanForDevice(
        adapter: android.bluetooth.BluetoothAdapter,
        wanted: String,
    ): BluetoothDevice = withContext(Dispatchers.IO) {
        val scanner = adapter.bluetoothLeScanner
            ?: throw IllegalStateException("蓝牙扫描器不可用")
        val found = CompletableDeferred<BluetoothDevice>()
        // 固件把服务 UUID 放进广播包, 用它过滤最可靠(名字可能被截断)
        val filters = listOf(
            ScanFilter.Builder().setServiceUuid(ParcelUuid(serviceUuid)).build(),
        )
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val found1 = result.device.name ?: ""
                if (found1.equals(wanted, ignoreCase = true) ||
                    found1.contains(wanted, ignoreCase = true)
                ) {
                    found.complete(result.device)
                }
            }

            override fun onScanFailed(errorCode: Int) {
                log("BLE: 扫描失败, errorCode=" + errorCode)
            }
        }
        scanner.startScan(filters, settings, callback)
        try {
            withTimeout(12_000) { found.await() }
        } catch (e: Exception) {
            throw IllegalStateException(
                "没扫到 BLE 设备 " + wanted + "。板子可能没上电, 或名字不对。"
            )
        } finally {
            try {
                scanner.stopScan(callback)
            } catch (_: Exception) {
            }
        }
    }

    // ---- GATT 回调 ----

    private val callback = object : BluetoothGattCallback() {

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    log("BLE: 链路建立但 status=" + status)
                }
                log("BLE: 链路建立, 协商 MTU…")
                // 关键: requestMtu 和 discoverServices 不能背靠背发。
                // Android 的 GATT 一次只允许一个未完成操作, 并发下发会让
                // 部分机型直接吞掉 onServicesDiscovered 回调。所以这里等
                // onMtuChanged 回来再发现服务, 并用定时器兜底。
                val requested = try {
                    g.requestMtu(MTU_REQUEST)
                } catch (e: Exception) {
                    log("BLE: requestMtu 异常 " + e.message)
                    false
                }
                if (requested) {
                    mtuFallbackJob = scope.launch {
                        delay(MTU_TIMEOUT_MS)
                        log("BLE: MTU 协商超时, 按默认 MTU 继续")
                        requestServices()
                    }
                } else {
                    requestServices()
                }
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                handleDisconnect("链路断开 (status=" + status + ")")
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, newMtu: Int, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS) {
                mtu = newMtu
                log("BLE: MTU 协商为 " + newMtu)
            } else {
                log("BLE: MTU 协商失败 status=" + status + ", 按 23 继续")
            }
            cancelMtuFallback()
            requestServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                handleDisconnect("发现服务失败, status=" + status)
                return
            }
            val service = g.getService(serviceUuid)
            if (service == null) {
                handleDisconnect("设备上没有目标服务, 固件版本可能不匹配")
                return
            }
            commandChar = service.getCharacteristic(commandUuid)
            val responseChar = service.getCharacteristic(responseUuid)
            if (commandChar == null || responseChar == null) {
                handleDisconnect("特征 UUID 不匹配")
                return
            }
            log("BLE: 服务已发现, 订阅通知…")
            g.setCharacteristicNotification(responseChar, true)
            val descriptor = responseChar.getDescriptor(CLIENT_CONFIG_UUID)
            if (descriptor == null) {
                log("BLE: 响应特征没有 CCCD, 跳过订阅(收不到设备回报)")
                finishConnect()
                return
            }
            val ok = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeDescriptor(descriptor, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) ==
                    BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                @Suppress("DEPRECATION")
                g.writeDescriptor(descriptor)
            }
            if (!ok) {
                log("BLE: 写 CCCD 失败, 仍然继续(只是收不到设备回报)")
                finishConnect()
            }
        }

        override fun onDescriptorWrite(
            g: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int,
        ) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("BLE: 订阅通知返回 status=" + status)
            }
            finishConnect()
        }

        override fun onCharacteristicWrite(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            synchronized(lock) { writing = false }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("BLE: 写入返回 status=" + status)
            }
            pump()
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
        ) {
            @Suppress("DEPRECATION")
            val data = characteristic.value ?: return
            appendNotification(data)
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            appendNotification(value)
        }
    }

    /** 固件按 180 字节分片通知, 用换行拼回整行。 */
    private fun appendNotification(data: ByteArray) {
        synchronized(notifyBuffer) {
            notifyBuffer.append(String(data, Charsets.UTF_8))
            while (true) {
                val index = notifyBuffer.indexOf(10.toChar())
                if (index < 0) break
                val line = notifyBuffer.substring(0, index).trim()
                notifyBuffer.delete(0, index + 1)
                if (line.startsWith("{")) emitReply(line)
            }
        }
    }

    // ---- 连接状态机 ----

    /** 发现服务 + 订阅通知, 只允许发起一次。 */
    private fun requestServices() {
        if (closed) return
        if (!servicesRequested.compareAndSet(false, true)) return
        val g = gatt ?: return
        try {
            if (!g.discoverServices()) {
                handleDisconnect("discoverServices 返回 false")
            }
        } catch (e: Exception) {
            handleDisconnect("discoverServices 异常: " + e.message)
        }
    }

    private fun cancelMtuFallback() {
        mtuFallbackJob?.cancel()
        mtuFallbackJob = null
    }

    /**
     * 全部就绪。注意 connected 必须在这里置位 ——
     * 之前这个字段从头到尾没被赋过 true, 于是 isConnected() 恒为 false,
     * 界面显示"已连接"但每一笔写入都被 pump() 挡掉, 日志刷"未连接, 命令未发出"。
     */
    private fun finishConnect() {
        if (closed) return
        if (ready.isCompleted) return
        connected = true
        ready.complete(Unit)
    }

    private fun handleDisconnect(reason: String) {
        val wasConnected = connected
        connected = false
        cancelMtuFallback()
        synchronized(lock) {
            writing = false
            writeQueue.clear()
        }
        if (ready.isCompleted) {
            if (wasConnected) {
                log("BLE: " + reason)
                emitState(false, label)
            }
        } else {
            ready.completeExceptionally(IllegalStateException(reason))
        }
    }

    // ---- 发送 ----

    override fun send(json: String) {
        if (!connected) return
        latest = (json + 10.toChar()).toByteArray(Charsets.UTF_8)
    }

    private fun startPump() {
        if (pumpJob != null) return
        pumpJob = scope.launch(Dispatchers.IO) {
            while (isActive) {
                val payload = latest
                if (payload != null && connected) {
                    latest = null
                    enqueue(payload)
                }
                delay(3)
            }
        }
        // 看门狗: 回调万一不来, writing 会永久卡住, 之后所有命令都发不出去
        watchdogJob = scope.launch(Dispatchers.IO) {
            while (isActive) {
                delay(1000)
                val stuck = synchronized(lock) {
                    writing && System.currentTimeMillis() - lastWriteAt > WRITE_TIMEOUT_MS
                }
                if (stuck) {
                    synchronized(lock) { writing = false }
                    log("BLE: 写入 " + WRITE_TIMEOUT_MS + "ms 无回调, 复位写状态")
                    pump()
                }
            }
        }
    }

    /** 按协商到的 MTU 切片(ATT 头占 3 字节)后排进写队列。 */
    private fun enqueue(payload: ByteArray) {
        val chunkSize = (mtu - 3).coerceIn(20, 512)
        synchronized(lock) {
            var offset = 0
            while (offset < payload.size) {
                val end = minOf(offset + chunkSize, payload.size)
                writeQueue.addLast(payload.copyOfRange(offset, end))
                offset = end
            }
        }
        pump()
    }

    /** 发下一片。任何时刻最多一笔在途, 由 onCharacteristicWrite 驱动续发。 */
    private fun pump() {
        val next = synchronized(lock) {
            if (writing || !connected) return
            val g = gatt ?: return
            val characteristic = commandChar ?: return
            val chunk = writeQueue.removeFirstOrNull() ?: return
            writing = true
            lastWriteAt = System.currentTimeMillis()
            Triple(g, characteristic, chunk)
        }
        val g = next.first
        val characteristic = next.second
        val chunk = next.third
        var ok = false
        var error: String? = null
        try {
            ok = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeCharacteristic(
                    characteristic, chunk, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                ) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                characteristic.value = chunk
                @Suppress("DEPRECATION")
                characteristic.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                @Suppress("DEPRECATION")
                g.writeCharacteristic(characteristic)
            }
        } catch (e: Exception) {
            error = e.message ?: e.toString()
        }
        if (!ok) {
            synchronized(lock) {
                writing = false
                writeQueue.clear()
            }
            log("BLE: writeCharacteristic 失败" + (error?.let { ": " + it } ?: " (返回 false)"))
        }
    }

    override fun close() {
        closed = true
        connected = false
        pumpJob?.cancel()
        pumpJob = null
        watchdogJob?.cancel()
        watchdogJob = null
        cancelMtuFallback()
        latest = null
        synchronized(lock) {
            writeQueue.clear()
            writing = false
        }
        val g = gatt
        gatt = null
        commandChar = null
        try {
            g?.disconnect()
            g?.close()
        } catch (_: Exception) {
        }
    }

    companion object {
        private val CLIENT_CONFIG_UUID: UUID =
            UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        /** 想要 517, 固件那边通知按 180 字节分片, 需要比默认 23 大得多。 */
        private const val MTU_REQUEST = 517
        private const val MTU_TIMEOUT_MS = 2500L
        private const val WRITE_TIMEOUT_MS = 4000L
    }
}
