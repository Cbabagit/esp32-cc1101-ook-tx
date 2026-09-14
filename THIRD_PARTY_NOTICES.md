# 第三方组件与来源声明

本仓库整体以 **GNU General Public License v3.0 only**（SPDX: `GPL-3.0-only`）发布，
全文见 [LICENSE](LICENSE)。

## 一、固件来源（重要）

`firmware/` 下的 ESP32-S3 桥接固件是 **Lightstick-Lab** 的派生作品：

- 上游项目：Lightstick-Lab
- 上游许可证：`GPL-3.0-only`
- 派生方式：以上游 `firmware/src/main.cpp` 为基础，修改了发射命令层

依据 GPL-3.0，本仓库的固件部分**必须**继续以 `GPL-3.0-only` 分发，并保留上游版权声明。
这是许可证义务，不是可选项。上游对本项目的具体授权情况请以你获取源码的渠道为准。

上游固件的设计原则是"**固件中不包含任何协议**"，本仓库继续保持该原则：
空口协议全部位于主机侧（`lightstick-player/protocol.py`），固件只提供通用的脉冲发射能力；
固件中与具体协议相关的两个函数是空占位符，见 README 的「空口协议」一节。

## 二、固件依赖

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| [ArduinoJson](https://github.com/bblanchon/ArduinoJson) | JSON 解析与序列化 | MIT |
| [SmartRC-CC1101-Driver-Lib](https://github.com/LSatan/SmartRC-CC1101-Driver-Lib) | CC1101 驱动 | MIT |
| [usb-host-msc](https://github.com/firechip/espressif32-lib-usb-host-msc) | USB 存储录制 | Apache-2.0 |

## 三、主机端依赖

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| [pyserial](https://github.com/pyserial/pyserial) | 串口传输 | BSD-3-Clause |
| [python-vlc](https://github.com/oaubert/python-vlc) | 视频播放与同步主时钟 | LGPL-2.1-or-later |
| [VLC / libVLC](https://www.videolan.org/vlc/) | python-vlc 的运行时（需自行安装） | GPL-2.0-or-later / LGPL-2.1-or-later |
| Tkinter | 图形界面（Python 标准库） | PSF |

> `python-vlc` 只做动态链接调用，不是本仓库的源码依赖。

## 四、构建工具链（仅构建依赖，源码未复制进本仓库）

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| [PlatformIO Core](https://github.com/platformio/platformio-core) | 构建与烧录工具 | Apache-2.0 |
| [platform-espressif32](https://github.com/platformio/platform-espressif32) | ESP32 平台包 | Apache-2.0 |
| [Arduino-ESP32](https://github.com/espressif/arduino-esp32) | ESP32 Arduino 框架 | LGPL-2.1-or-later |
| [ESP-IDF](https://github.com/espressif/esp-idf) | 底层 SDK | Apache-2.0 |
| [FFmpeg / ffprobe](https://ffmpeg.org/) | 视频元数据探测（需自行安装） | LGPL-2.1-or-later 或 GPL（取决于构建选项） |

上表只记录顶层许可证；各发行包内还可能带有自己的子组件声明，以各自上游为准。

## 五、关于空口协议

本仓库**不发布**任何空口协议的规范文档，也不声明对该协议拥有任何权利。
`lightstick-player/protocol.py` 中是本项目为主机侧控制而实现的一份协议代码，
其参数（频率、符号宽度、前导码、位模板、帧结构）均由使用者自行确认合规后再使用。

本项目与任何应援棒、灯具、活动主办方、艺人或游戏厂商均无关联，
未获其授权、赞助或背书。相关名称与商标归各自所有者所有。
