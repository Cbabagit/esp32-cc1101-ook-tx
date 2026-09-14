# ESP32-S3 + CC1101 OOK 发射套件

简单来说就是把 **433 MHz OOK/ASK 脉冲**送到空中的一套工具。分两部分：

- `lightstick-player` —— PC 端播放器。按时间轴把 CSV 序列变成发射命令，通过串口下发给桥接固件，并可同步播放本地视频。
- `firmware` —— ESP32-S3 桥接固件。用 RMT 产生微秒级精确的 OOK 波形，由 CC1101 发出去。**固件只提供通用发射框架，不含具体协议。**

> 本仓库的固件是 **Lightstick-Lab**（`GPL-3.0-only`）的派生作品，因此整体以 `GPL-3.0-only` 发布。
> 详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 免责声明

本项目是无线电与嵌入式学习用途的工具，**仅供实验和学习**。

- 请遵守你所在国家/地区的无线电法规；未经许可发射可能违法。
- 请在**低功率、近距离**条件下验证。默认发射功率 -20 dBm，不要擅自加大。
- 空口协议部分由使用者自行确认合规后使用；本仓库不声明对任何第三方协议拥有权利。
- 本项目与任何灯具、应援棒、活动主办方、艺人或游戏厂商均无关联，未获其授权或背书。

## 目录结构

```text
esp32-cc1101-ook-tx/
├── lightstick-player/        PC 端播放器 (Python)
│   ├── protocol.py           空口协议实现  ★ 协议在这里
│   ├── main.py               命令行播放器
│   ├── gui.py                Tkinter 图形界面 (含全屏)
│   ├── csv_loader.py         CSV 解析
│   ├── timeline.py           时间轴调度 (限速 / 丢帧保同步)
│   ├── transport.py          串口传输 (后台写线程 + 合帧)
│   ├── video.py              libVLC 封装 + 窗口搬家
│   ├── d8.py                 向后兼容 shim
│   ├── examples/             示例 CSV 与测试视频
│   ├── tools/                硬件实测与诊断脚本
│   └── tests/                单元测试与真机测试
├── firmware/                 ESP32-S3 桥接固件 (PlatformIO)
│   ├── platformio.ini
│   ├── boards/               自定义板卡定义 (N16R8)
│   └── src/
│       ├── main.cpp          命令解析 + RMT 发射 + 通用空口编码
│       └── recorder.cpp/.h   USB 存储录制
├── LICENSE                   GPL-3.0-only
└── THIRD_PARTY_NOTICES.md
```

## 硬件

| 部件 | 说明 |
| --- | --- |
| 控制板 | ESP32-S3-DevKitC-1 **N16R8**（16 MB Flash、8 MB OPI PSRAM） |
| 射频模块 | CC1101 模块（433 MHz 频段） |
| 主机 | Windows / macOS / Linux，Python 3.10+ |

CC1101 使用 **3.3 V** 逻辑与供电，**不能接 5 V**。ESP32-S3 与 CC1101 必须共地。

| CC1101 引脚 | ESP32-S3 GPIO |
| --- | --- |
| SCK | GPIO4 |
| MOSI (SI) | GPIO5 |
| MISO (SO) | GPIO6 |
| CSN (CS) | GPIO7 |
| GDO0 | GPIO15 |
| GDO2 | GPIO16 |
| VCC | 3V3 |
| GND | GND |

> 引脚定义在 `firmware/src/main.cpp` 顶部的 `PIN_SCK` 等常量里，换接线改这里即可。

## 播放器 lightstick-player

### 安装

```bash
cd lightstick-player
pip install -r requirements.txt   # pyserial, python-vlc
```

视频同步还需要本机装有 **VLC**（libVLC）。不装也能用，只是没有视频功能。

### 命令行

```bash
python main.py --list-ports                       # 列出串口
python main.py show.csv --dry-run                 # 只看命令, 不接硬件
python main.py show.csv --port COM10              # 播放
python main.py show.csv --port COM10 --video a.mp4 --fullscreen   # 配视频全屏

# 帧间隔 <100ms 的序列
python main.py show.csv --port COM10 --mode c0 --drop-late
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--mode {d8,c0,zone}` | d8 | 走 protocol.py 里的哪种帧编码 |
| `--burst {1,2,3,6}` | 6 | d8 模式每次发几帧 |
| `--bit-us N` | 250 | 符号宽度（微秒） |
| `--repeat N` | 1 | c0/zone 每帧重复次数 |
| `--drop-late` | 关 | 跟不上的帧直接跳过（保持与视频同步），而不是排队等待 |
| `--legacy` | 关 | 改走 TX_PULSES + durations_us（慢，仅用于对照） |

### 图形界面

```bash
python gui.py
```

加载 CSV + 视频 + 连接串口 + 播放/暂停/停止 + 灯光延迟 + 10 通道实时预览 + 时间轴进度 + 日志。

**全屏播放视频**：`⛶ 全屏` 按钮 / `播放时全屏` 勾选框 / `F11` 切换 / `Esc` 退出 / 双击预览画面。全屏与嵌入预览之间来回切换时画面不中断、时间轴不倒退。

### 打包成 Windows exe

```bash
cd lightstick-player
pip install pyinstaller
py build_exe.py            # 打两个 exe, 单文件模式
py build_exe.py --onedir   # 单目录模式 (启动快很多, 但要带整个文件夹)
py build_exe.py --both-modes   # 两种都出
```

产物在 `dist/`：

| 文件 | 说明 |
| --- | --- |
| `lightstick-player.exe` | 图形界面版。无控制台，双击即用 |
| `lightstick-player-cli.exe` | 命令行版。带控制台，`--list-ports` / 播放等 |

**为什么是两个**：`--windowed` 的进程没有 stdout，命令行模式会变成哑巴；`--console` 的进程双击时会多一个黑框。分开打各自都正常。

#### 单文件 vs 单目录（实测差 10 倍）

| 模式 | 启动耗时 | 体积 | 分发方式 |
| --- | --- | --- | --- |
| 单文件（默认） | **5.0 ~ 6.0 秒** | 每个 11.5 MB | 拷一个 exe 就行 |
| 单目录（`--onedir`） | **0.5 秒**（首次 1.4 秒） | 16.1 MB / 953 个文件 | 要拷整个文件夹 |

单文件每次运行都要把 11.5 MB 解压到 `%TEMP%`，所以慢。**自己日常用建议单目录**，
打包给别人用再上单文件。两种模式可以一起产出（互不覆盖，单目录版在子目录里）：

```bash
py build_exe.py --both-modes
```

在没装 Python 的机器上都能直接跑。

目标机器跑不起来时，先让对方执行这一条，环境问题一目了然：

```bash
lightstick-player-cli.exe --selftest
```

它会报告 tkinter / pyserial / libVLC / ffprobe 是否就绪。

#### 打包时踩到的坑（`build_exe.py` 里都处理了）

1. **Python 3.14 的 Tcl/Tk 脚本库是 zip 形式内嵌的**。Tcl 9 通过 zipfs 挂载它，
   `info library` 返回 `//zipfs:/lib/tcl/tcl_library` 这种虚拟路径，PyInstaller 用
   `os.path.isdir()` 判断数据目录是否存在，必然为假，于是 `_tcl_data` / `_tk_data` 一个都没打进包，
   exe 一启动就报 `FileNotFoundError: Tcl data directory ... not found`。
   解决办法：把 `libtcl*.zip` / `libtk*.zip` 解开，手动作为 `_tcl_data` / `_tk_data` 加进去。
2. **暂存目录不能放在 PyInstaller 的 workpath 里**。`--clean` 会清空整个 workpath，
   于是第一个 exe 打完，第二个就找不到刚解出来的 Tcl 数据了。现在放在 `%TEMP%` 下。
3. **一次性打包在 Windows 上是两个进程**（引导器 + 真正的 Python 进程），
   窗口属于子进程。写脚本验证窗口有没有起来时要查子进程，别只看 `Start-Process -PassThru` 拿到的那个。

> 视频同步依赖目标机器装有 **VLC**（libVLC），视频元数据探测依赖 **ffprobe**。
> 两者都不装也能用，只是没有视频功能 —— exe 里没有捆绑它们。

### CSV 格式

| 列 | 说明 |
| --- | --- |
| `frame_time_ms` | 帧时间戳（毫秒），主时钟相对起点 |
| `frame_id` | 顺序编号 |
| `frame_type` | 帧类型（blackout/color/rainbow…，仅元数据） |
| `ch{N}_function` | 通道功能码 0-3 |
| `ch{N}_red/green/blue` | 每分量 0-15（实际 RGB = 值 × 17） |

N = 0..9。示例见 `lightstick-player/examples/demo.csv`。

### 帧间隔下限（实测）

空口帧长 = (前导符号数 + 字节数 × 8 × 每数据位符号数) × 符号宽度，**这决定了最快能播多快**。
实测某类接收端在 200 µs 符号宽度下仍能解码、190 µs 及以下不再解码。
该下限取决于**接收端**而不是发射端，换设备请自行重测。

| 载荷 | 符号数 | 符号宽度 | 单帧空口 | 实测往返 |
| --- | --- | --- | --- | --- |
| 21 字节 | 521 | 250 µs | 130.2 ms | ~143 ms |
| 21 字节 | 521 | 200 µs | 104.2 ms | ~117 ms |
| 7 字节 | 185 | 250 µs | 46.3 ms | ~64 ms |

固定开销约 15 ms/条（CC1101 SPI + 校准 + RMT 安装卸载 + 串口往返）。

## 固件

### 编译与烧录

```bash
cd firmware
pio run                                  # 编译
pio run -t upload --upload-port COM10    # 烧录 (Linux/macOS 用 /dev/ttyUSB0)
pio device monitor -b 921600             # 可选: 看串口输出
```

首次编译要下载工具链，耗时较长；之后是增量编译。

### 通信方式

串口 **921600 8N1**，或者 BLE / Wi-Fi HTTP，三者共用同一套 JSON。
每条命令一行 JSON 以换行结尾，响应也是一行 JSON，靠 `id` 对应：

```json
{"id":"1","cmd":"TX_D8","args":{ ... }}
```

发射类命令是**先确认、后完成**的两段式响应：

```json
{"id":"1","ok":true,"result":{"status":"pending","accepted":true,"duration_us":785750}}
{"id":"1","ok":true,"event":"TX_COMPLETE","result":{"status":"success","pulse_count":2040}}
```

也可以用 `GET_TX_RESULT` 事后查询结果。

### TX_D8 —— 九槽帧发射

主机把 9 个 16bit 槽字发给固件，固件在板内组帧并连发若干帧。
命令载荷约 170 字节，比把上千个时长写进 JSON 小两个数量级。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `slots` | int[9] | 必填 | 9 个 16bit 槽字 |
| `burst` | int | 6 | 连发几帧，取值 1 / 2 / 3 / 6 |
| `bit_us` | int | 250 | 每个符号的宽度（微秒），20..2000 |
| `gap_us` | int | 850 | 帧与帧之间保持低电平的时长 |
| `frequency_hz` | int | 433920000 | 载波频率 |
| `power_dbm` | int | -30 | 发射功率，-30..10 |

### TX_BYTES —— 直接发帧字节

主机发帧字节的 hex，固件负责把字节编码成空口波形后发射。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `data_hex` | 必填 | 偶数长度的 hex 字符串，1..64 字节 |
| `bit_us` | 250 | 符号宽度 |
| `repeat` | 1 | 重复次数，1..10 |
| `gap_us` | 20000 | 重复之间的低电平 |

### TX_PULSES —— 直接给时长

不想自己编码时，可以直接给一串高/低交替的微秒时长，固件原样输出：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `durations_us` | 必填 | 1..2048 个时长，每个 10..100000 µs，起始为高电平 |
| `repeat` | 1 | 1..10 |
| `gap_us` | 20000 | 重复之间的低电平 |
| `start_level` | 0 | 起始电平 |


### 其它命令

状态与调试：`GET_INFO`、`GET_STATUS`、`GET_COMMANDS`、
`GET_TX_RESULT`、`ABORT`、`CC_RESET`、`CC_SELF_TEST`、
`REG_READ` / `REG_WRITE` / `STROBE` / `FIFO_READ` / `FIFO_WRITE`。

收发与录制：`TX_PACKET`、`RX_PACKET`、`RX_PULSES`、
`START_RECORDING` / `STOP_RECORDING` / `LIST_RECORDINGS` 等。

信号 Profile：`SET_PROFILE` / `GET_PROFILE` / `LIST_PROFILES` / `DELETE_PROFILE`，
可以把一组发射参数存进 NVS，之后按名字复用。

联网：`SET_WIFI_CONFIG` / `CONNECT_WIFI` / `GET_NETWORK_STATUS`。

完整列表直接问板子：`{"id":"1","cmd":"GET_COMMANDS"}`。

### 限制

| 限制 | 值 |
| --- | --- |
| 单段波形时长 | ≤ 1000 ms |
| 单段脉冲条数 | ≤ 2048 |
| 单帧字节数 | ≤ 64 |
| 单帧符号数 | ≤ 8192 |

## 空口协议（★ 提供的代码里没有这部分实现）

**重要：本仓库发布的固件不含任何具体空口协议。**

固件里与协议相关的只有两个函数，它们现在是**空占位符**：

| 位置 | 原职责 | 现在的状态 |
| --- | --- | --- |
| `buildD8Frame()` | 把 9 个槽字 + phase 组装成 21 字节帧（帧头 / 槽字旋转打包 / 校验和） | 占位，memset 成全 0 |
| `appendFrameDurations()` | 把帧字节编码成 RMT 时长（前导码 + 逐位展开 + 游程编码） | 占位，不产出任何时长 |

对应的常量也是占位的：`D8_SLOT_COUNT`、`D8_FRAME_BYTES`、`D8_PHASE_SEQUENCE`。
**在你补全这两个函数之前，`TX_D8` 和 `TX_BYTES` 不会产生任何有意义的发射。**
通用部分（JSON 参数解析、任务队列、RMT 波形输出、CC1101 配置）都是完整的。

下面把协议本身写出来，方便你对照着实现 —— 这些内容来自对某款 433 MHz 应援棒的实测分析，

### 物理层

| 项 | 值 |
| --- | --- |
| 调制 | ASK / OOK |
| 频率 | 433.920 MHz |
| 符号宽度 | 250 µs |
| 起始电平 | 高 |

### 帧的比特编码

每帧的比特流由两段拼成：

1. **前导码**：`11111111000011110`（17 个符号，原样发射）
2. **数据段**：对帧里每个字节，从 MSB 到 LSB，每个数据位 d 展开成三个符号 `0, 1, d`

所以 21 字节帧 = 17 + 21 × 8 × 3 = **521 个符号**，在 250 µs 下是 130.25 ms。

```text
比特流 = 前导码 + 对每个数据位 d 追加 "0" "1" d

然后把整串符号做游程编码: 相邻同电平合并成一段, 得到交替的高低持续时长,
交给 RMT 输出。注意时长列表的奇偶性必须保持交替, 否则会把相邻两个高电平
当成「高 + 低」。
```

### D8 帧格式（21 字节）

```text
偏移  内容
0     0xD8                                  帧类型
1..18 9 个槽字, 每个 16bit 大端, 值 = ROL16(slot, 2)
19    PHASE                                 帧阶段号
20    K = (0x96 + 前 20 字节之和) & 0xFF       校验和
```


### 连发与阶段

一次事务连发 6 帧，PHASE 依次为 `2, 1, 0, 2, 1, 0`；第二组是冗余，用来提高可靠率。
帧与帧之间保持 850 µs 低电平，帧起始间隔 131.1 ms，整段约 786 ms。

### 其它帧类型

| 命令 | 帧布局 | 用途 |
| --- | --- | --- |
| `00` | `00 M1 M2 X S C K`（7 字节） | 分区颜色/状态（16 色调色板 + 状态码） |
| `C0` | `C0 AB CD EF GH IJ K`（7 字节） | 十个分区各自的 4bit 色码 |
| `A6` | `A6 C K`（3 字节） | 全局短脉冲 |
| `DA` | `DA M1 M2 X1 X2 X3 K`（7 字节） | 实体按键解锁 |

校验算法对所有帧都一样：`(0x96 + 前面所有帧字节之和) & 0xFF`。

### 主机侧参考实现

本项目把协议的**帧构造**放在主机侧，`lightstick-player/protocol.py` 就是上面这些帧的
一份 Python 实现（含 16 色调色板、槽字打包、ROL16、校验和、爆发拼接）。
固件只负责把主机给它的字节发出去。

> 如果你打算只发布通用框架，把 `lightstick-player/protocol.py` 也换成占位符即可，
> 固件侧不需要任何改动。

## 测试

```bash
cd lightstick-player
python tests/test_core.py        # 108 项: CSV / 协议帧构造 / 空口编码 / 时间轴 / 紧凑命令层
python tests/test_gui_smoke.py   # GUI 冒烟 (需要显示环境)
python tests/test_fullscreen.py  # 真起 VLC 验证 嵌入<->全屏 切换 (需要 VLC)
                                 # 用的是 examples/testclip.mp4 (ffmpeg 生成的测试片)
```

`lightstick-player/tools/` 下还有几个真机用的脚本：`port_state.py`（串口是否可用）、
`quick_sweep.py`（逐档点亮看接收端反应）、`vlc_hwnd_probe.py`（排查视频输出黑屏）。

## 许可证

[GPL-3.0-only](LICENSE)。

固件派生自 Lightstick-Lab（同样 `GPL-3.0-only`），因此本仓库必须沿用该许可证并保留上游署名，
不能改成 MIT / Apache。第三方组件清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 致谢

- Lightstick-Lab —— 固件基础与硬件设计思路
- [SmartRC-CC1101-Driver-Lib](https://github.com/LSatan/SmartRC-CC1101-Driver-Lib) —— CC1101 驱动
- [ArduinoJson](https://github.com/bblanchon/ArduinoJson) —— JSON 解析
- [python-vlc](https://github.com/oaubert/python-vlc) —— 视频播放与同步主时钟
- [Lightstick-Lab](https://github.com/AcosX/Lightstick-Lab) 
- [Lumaflow](https://github.com/ltyridium/LumaFlow)