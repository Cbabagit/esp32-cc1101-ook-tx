# 应援棒播放器 (Lightstick Player)

ESP32-S3 + CC1101 应援棒控制板的 Android 配套控制端。

- 包名 `com.lightstick.player`，版本 0.1.0 (versionCode 1)
- minSdk 26 (Android 8.0) / targetSdk 37 / compileSdk 37
- 界面语言：简体中文 / 繁體中文 / English（跟随系统）
- 主题色 `#66CCFF`

---

## 一、这是什么

Python GUI 上位机的 Android 端对应物。Android 端只负责**发射控制与编排**，
真正的 433.920 MHz OOK 波形仍然由 ESP32 固件生成 —— App 只把「要发什么」发过去。

| 通道 | 说明 | 状态 |
| --- | --- | --- |
| **Wi-Fi UDP** | 广播发现 + JSON 命令，端口 4210，单包 ≤ 1400 字节 | 已实现，**未在真机上联调过** |
| **蓝牙 BLE** | GATT 写入，服务 `8f7a0001-4c53-4331-9638-53334e313652`，命令特征 `…0002`，响应特征 `…0003` | 已实现 |
| **HTTP** | 板载 HTTP 接口 | 已实现 |
| **USB OTG 串口** | 手机直连开发板 | **仅保留入口，未实现**（见第七节） |

---

## 二、编译与安装

### 环境

- JDK 25（用 Android Studio 自带的 JBR 即可）
- Gradle 9.7.1 + AGP 9.4.1
  - ⚠️ AGP 9 已内置 Kotlin 支持，**不要**再加 `org.jetbrains.kotlin.android` 插件，否则直接报错
  - 只需要 `org.jetbrains.kotlin.plugin.compose`
- Android SDK：platform `android-37`、build-tools `36.0.0`

### 编译

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
cd C:\Users\C_bab\.dsh\lightstick-android
& C:\Users\C_bab\.dsh\tools\gradle\gradle-9.7.1\bin\gradle.bat assembleDebug --console=plain
```

产物 `app/build/outputs/apk/debug/app-debug.apk`（约 18 MB），已复制一份到 `dist/lightstick-player-0.1.0-debug.apk`。

### 安装

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" install -r dist\lightstick-player-0.1.0-debug.apk
```

或者直接把 apk 拷进手机点安装（需要允许「未知来源」）。

### 单元测试

```powershell
& C:\Users\C_bab\.dsh\tools\gradle\gradle-9.7.1\bin\gradle.bat testDebugUnitTest --console=plain
```

`ProtocolTest` 里的期望值是用 Python 端的 `_gen_golden.py` 生成的真值比对过的，
也就是说 Kotlin 的协议实现和「已经在真实板子上跑通的 Python 实现」逐字节一致。

---

## 三、界面结构

底部四个标签页。

### 1. 实时 (Live) —— 手动点灯

- **连接卡片**：选通道（Wi-Fi / 蓝牙 / HTTP / USB）、填地址、连接 / 断开、显示状态与设备信息。
- **参数卡片**：两路颜色（色轮 + 16 色调色板）、亮度、模式 (Mode)、通道开关。
- **9 个槽位**：每个槽位独占一行，可单独开 / 关、调色、调亮度。
- **操作按钮**：`下发当前状态`（把整个界面状态一次性推给板子）、`黑场`、`终端`。

### 2. 编排 (Sequence) —— 播放 CSV 序列

载入 CSV 文件后可以播放 / 暂停 / 停止。CSV 列的语义和 Python 端 `csv_loader.py` 完全一致，
解析逻辑已经移植成 `CsvParser.kt`。

当前是 **主机节拍 (host-paced)**：App 按 CSV 里的时间戳逐条下发。
「设备节拍」（把整段序列丢给板子自己跑）已列入计划但还没做，见第七节。

### 3. 设备 (Device) —— 状态与日志

读取板子信息（固件版本、Wi-Fi 状态、IP 等），以及一个滚动日志面板，
能看到每一条下发的命令和板子的回包（含 `TX_COMPLETE` 异步回报）。

### 4. 编辑 (Edit)

占位页，里面写清楚了这个页面**打算**做什么。

---

## 四、Wi-Fi 通道怎么用（这段最容易踩坑，务必看）

板子和手机必须**在同一个网段**，而且路由器不能开「AP 隔离」。

1. 先在板子那侧连上 Wi-Fi（串口 / 配置文件配好 SSID 和密码）。
2. App 里选 Wi-Fi 通道，地址填 **广播地址**（例如 `192.168.1.255`）或板子的 IP。
3. 点连接。App 会先发 `LIGHTSTICK_DISCOVER` 广播，收到回复后自动填上真实 IP 和端口。

**已知的两个坑：**

- 如果电脑 / 手机上开了 Clash、VPN 之类的 TUN 模式代理，广播包会被吃掉，
  表现为「一直发现不到设备」。此时要么关掉代理，要么直接在地址栏手填板子 IP。
- Android 上走 UDP 广播需要先 `bindSocket` 到 Wi-Fi 网络接口，否则包出不去。
  App 里已经处理了（`UdpTransport` 里用 `Network.bindSocket` 绑定），
  但如果手机同时插着流量卡又连 Wi-Fi，还是可能被路由到移动网络 —— 关掉流量即可。

---

## 五、蓝牙通道怎么用

1. 板子上电后 BLE 广播名是 `Lightstick N16R8`。
2. Android 12 及以上需要**附近设备**权限，App 首次使用时会弹窗申请（选「允许」）。
3. 在连接卡片里点「扫描」→ 选中设备 → 连接。

BLE 的单包写入有 MTU 限制（协商后 517，实际按 180 字节分块），
`BleTransport` 里已经做了分块和写入队列，超长命令会自动拆包按序发送。

---

## 六、终端

实时页右上角的「终端」按钮打开一个输入框，可以直接手打 JSON 命令发给板子，
用来调试固件里那些 App 还没做成按钮的命令。

发出去的原文和收到的回包都会进日志面板。

---

## 七、目前**没有**做的部分（如实说明）

1. **音视频同步** —— 你要求过，还没实现。计划用 media3/ExoPlayer 当主时钟，
   再按时间轴驱动灯光序列。
2. **设备节拍播放** —— 现在是手机逐条下发，手机一卡顿时序就抖。
   要做得让板子自己跑整段序列，这需要**动固件**（新增一条「下发整段序列」的命令）。
3. **USB OTG 串口** —— 只留了入口，点了会明确提示未实现。
   真要做需要 `usb-serial-for-android` 库 + 处理 OTG 供电、权限弹窗、CDC 握手，比较麻烦，
   所以按你说的「demo 里仅保留选项即可」处理。
4. **编辑页** —— 占位，编排逻辑按你说的另做。

### 验证到什么程度了

- ✅ 编译通过（`BUILD SUCCESSFUL`），单元测试全过
- ✅ APK 里三种语言资源确实打进去了（`aapt2 dump badging` 能看到 `zh-CN` / `zh-TW` 和中文应用名）
- ✅ 协议实现与 Python 真值逐字节一致
- ❌ **从没在真机或模拟器上跑过** —— 这台机器上没有连手机，也没装任何模拟器镜像。
  所以所有界面交互、权限弹窗、UDP 广播、BLE 连接都是「写完了但没跑过」的状态。

装到手机上第一次跑，大概率还要修几个运行时问题，属正常。
