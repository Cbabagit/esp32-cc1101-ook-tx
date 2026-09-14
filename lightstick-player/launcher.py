# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""lightstick-player 统一入口 (打包成 exe 用)。

不带参数启动图形界面; 带参数按命令行模式跑, 参数原样转给 main.py。
也可以显式指定:

    lightstick-player.exe                    启动图形界面
    lightstick-player.exe --cli show.csv ... 命令行模式
    lightstick-player.exe show.csv --port COM10
    lightstick-player.exe --list-ports

直接跑源码时请用 main.py / gui.py, 这个文件只为打包存在。
"""
from __future__ import annotations

import sys


def _make_stdout_safe() -> None:
    """控制台编码可能是 GBK/CP437, 直接 print 中文会抛 UnicodeEncodeError 把
    整个命令打断。这里只放宽错误处理, 不改编码 —— 改了反而会在中文控制台上花屏。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def selftest_lines():
    """检查运行环境。打包发出去之后, 这是排查"对方机器上跑不起来"的第一手段。"""
    import os
    import platform

    lines = []
    frozen = getattr(sys, "frozen", False)
    lines.append("打包运行: %s" % ("是" if frozen else "否 (源码)"))
    lines.append("Python  : %s (%s)" % (sys.version.split()[0], platform.machine()))
    lines.append("可执行  : %s" % sys.executable)
    encoding = getattr(sys.stdout, "encoding", None)
    lines.append("stdout  : %s (encoding=%s)" % ("有" if sys.stdout else "无", encoding))

    try:
        import tkinter
        lines.append("tkinter : OK (Tk %s)" % tkinter.TkVersion)
    except Exception as exc:
        lines.append("tkinter : 失败 - %s" % exc)

    try:
        import serial
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
        lines.append("pyserial: OK (%s), 串口 %s" % (serial.__version__, ports or "无"))
    except Exception as exc:
        lines.append("pyserial: 失败 - %s" % exc)

    try:
        import vlc
        lines.append("python-vlc: OK (%s)" % getattr(vlc, "__version__", "?"))
        try:
            instance = vlc.Instance()
            version = vlc.libvlc_get_version()
            if isinstance(version, bytes):
                version = version.decode("utf-8", "replace")
            lines.append("libVLC  : OK (%s)" % version)
            instance.release()
        except Exception as exc:
            lines.append("libVLC  : 失败 - %s" % exc)
            lines.append("          视频功能不可用; 需要安装 VLC (https://www.videolan.org/)")
    except Exception as exc:
        lines.append("python-vlc: 失败 - %s" % exc)

    try:
        import shutil
        probe = shutil.which("ffprobe")
        lines.append("ffprobe : %s" % (probe or "未找到 (视频元数据探测不可用)"))
    except Exception as exc:
        lines.append("ffprobe : 失败 - %s" % exc)

    return lines


def run_selftest() -> int:
    text = chr(10).join(selftest_lines())
    try:
        print(text)
        sys.stdout.flush()
    except Exception:
        pass
    if sys.stdout is None:
        _fatal(text)          # 无控制台的打包版本, 用弹窗展示
    return 0


def run_gui() -> int:
    import gui
    gui.main()
    return 0


def run_cli(argv) -> int:
    import main
    return main.main(list(argv))


def main() -> int:
    _make_stdout_safe()
    argv = list(sys.argv[1:])
    if argv and argv[0] == "--selftest":
        return run_selftest()
    if argv and argv[0] == "--gui":
        return run_gui()
    if argv and argv[0] == "--cli":
        return run_cli(argv[1:])
    if argv:
        return run_cli(argv)
    return run_gui()


def _fatal(message: str) -> None:
    """windowed(无控制台) 打包时异常是看不见的, 这里弹窗兜底。"""
    try:
        import tkinter.messagebox as messagebox
        messagebox.showerror("lightstick-player", message)
    except Exception:
        sys.stderr.write(message + chr(10))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        _fatal("启动失败:" + chr(10) + chr(10) + traceback.format_exc())
        sys.exit(1)
