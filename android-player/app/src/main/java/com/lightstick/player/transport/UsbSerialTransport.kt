package com.lightstick.player.transport

import com.lightstick.player.data.TransportKind

/**
 * USB OTG 串口。
 *
 * 目前只保留界面入口, 没有实现。原因:
 *
 *  1. 安卓没有内置 USB 串口支持, 需要引入第三方库(通常是
 *     com.github.mik3y:usb-serial-for-android), 并且要为 CH343 这类型号准备驱动。
 *  2. 手机/平板的 OTG 供电和线材兼容性差异很大, 现场可靠性不如 BLE 和 Wi-Fi。
 *  3. 需要额外处理 USB 权限弹窗、设备插拔广播、前台服务保活。
 *
 * 要补上的话, 大致是:
 *  - 加依赖 usb-serial-for-android
 *  - AndroidManifest 里加 <intent-filter> 监听 USB_DEVICE_ATTACHED, 或运行时申请权限
 *  - 用 UsbSerialPort 以 921600 8N1 打开, 实现和 SerialTransport(PC 端) 一样的读写循环
 *
 * 在此之前, 这条路径会明确告诉用户尚未实现, 而不是假装连上了。
 */
class UsbSerialTransport : Transport() {

    override val kind = TransportKind.USB

    override fun describe(): String = "usb (未实现)"

    override suspend fun open() {
        throw UnsupportedOperationException(
            "USB OTG 串口在安卓端还没有实现。请改用 BLE 或 Wi-Fi UDP; " +
                "有线连接请先用 PC 端的 lightstick-player。"
        )
    }

    override fun send(json: String) {
        // 不会走到这里: open() 已经抛异常了
    }

    override fun isConnected(): Boolean = false
}
