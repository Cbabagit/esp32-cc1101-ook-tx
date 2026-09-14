# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
"""用 PyInstaller 把 lightstick-player 打包成 Windows exe。

默认打两个:

    dist/lightstick-player.exe       图形界面版 (无控制台, 双击即用)
    dist/lightstick-player-cli.exe   命令行版 (带控制台, --list-ports / 播放等)

为什么是两个: --windowed 的进程没有 stdout, 命令行模式会变成"哑巴";
--console 的进程双击时会多一个黑框。分开打各自都正常。

用法:

    py build_exe.py                # 两个都打 (默认, 单文件)
    py build_exe.py --gui-only     # 只打图形界面版
    py build_exe.py --cli-only     # 只打命令行版
    py build_exe.py --onedir       # 单目录模式 (启动快很多, 但要整个文件夹)
    py build_exe.py --clean        # 先清掉 build/dist

注意: 视频同步需要目标机器装有 VLC (libVLC)。不装也能用, 只是没有视频功能。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "launcher.py")

# pyserial / python-vlc 里有运行时才导入的模块, 静态分析容易漏
HIDDEN_IMPORTS = [
    "vlc",
    "serial",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
]


def prepare_tcl_tk(staging_root: str):
    """准备 Tcl/Tk 脚本库, 返回 [(源目录, 打包目标名), ...]。

    Python 3.14 的 Windows 发行版把 Tcl/Tk 的脚本库打成了 zip, Tcl 9 通过 zipfs
    挂载它 (info library 返回 //zipfs:/lib/tcl/tcl_library)。PyInstaller 用
    os.path.isdir() 判断这个路径, 必然为假, 于是 _tcl_data / _tk_data 一个都
    没打进去, exe 一启动就 FileNotFoundError。这里把 zip 解开, 手动补上。
    """
    import glob
    import zipfile

    tcl_root = os.path.join(os.path.dirname(sys.executable), "tcl")
    specs = [
        ("libtcl*.zip", "tcl_library", "tcl", "_tcl_data"),
        ("libtk*.zip", "tk_library", "tk", "_tk_data"),
    ]
    result = []
    for pattern, prefix, folder, dest in specs:
        matches = sorted(glob.glob(os.path.join(tcl_root, pattern)))
        if not matches:
            print("!! 找不到 %s (在 %s 下)" % (pattern, tcl_root), flush=True)
            continue
        target = os.path.join(staging_root, folder)
        shutil.rmtree(target, ignore_errors=True)
        tmp = target + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(matches[-1]) as archive:
            archive.extractall(tmp)
        inner = os.path.join(tmp, prefix)
        os.makedirs(target, exist_ok=True)
        for entry in os.listdir(inner):
            shutil.move(os.path.join(inner, entry), os.path.join(target, entry))
        shutil.rmtree(tmp, ignore_errors=True)
        count = sum(len(files) for _r, _d, files in os.walk(target))
        print("Tcl/Tk 数据: %s -> %s (%d 个文件)" % (os.path.basename(matches[-1]), dest, count),
              flush=True)
        result.append((target, dest))
    return result


def build(name: str, windowed: bool, onedir: bool, datas) -> int:
    import PyInstaller.__main__ as pyi

    args = [
        ENTRY,
        "--name", name,
        "--noconfirm",
        "--clean",
        "--onedir" if onedir else "--onefile",
        "--windowed" if windowed else "--console",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        "--paths", HERE,
    ]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    for src, dest in datas:
        args += ["--add-data", src + os.pathsep + dest]

    icon = os.path.join(HERE, "app.ico")
    if os.path.exists(icon):
        args += ["--icon", icon]

    print("=== PyInstaller:", " ".join(args), flush=True)
    pyi.run(args)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui-only", action="store_true")
    parser.add_argument("--cli-only", action="store_true")
    parser.add_argument("--onedir", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--both-modes", action="store_true",
                        help="单文件和单目录两种模式都产出 (默认只出单文件)")
    args = parser.parse_args()

    for folder in ("build", "dist"):
        path = os.path.join(HERE, folder)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            print("removed", folder)
    if args.clean:
        return 0

    # 暂存放 %TEMP%: PyInstaller 的 --clean 会清空 workpath, 放 build/ 里会被带走
    import tempfile
    staging = tempfile.mkdtemp(prefix="lightstick-player-tcltk-")
    try:
        datas = prepare_tcl_tk(staging)

        modes = [args.onedir]
        if args.both_modes:
            modes = [False, True]
        for onedir in modes:
            if not args.cli_only:
                build("lightstick-player", windowed=True, onedir=onedir, datas=datas)
            if not args.gui_only:
                build("lightstick-player-cli", windowed=False, onedir=onedir, datas=datas)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    dist = os.path.join(HERE, "dist")
    print()
    print("=== 产物 ===")
    for root, _dirs, files in os.walk(dist):
        for name in sorted(files):
            if name.lower().endswith(".exe"):
                full = os.path.join(root, name)
                print("  %-46s %6.1f MB" % (os.path.relpath(full, HERE),
                                            os.path.getsize(full) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
