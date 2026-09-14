# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""GUI 冒烟测试: 构建 UI + 加载 CSV + 更新通道预览(需要显示环境)。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
import gui as gui_mod

root = tk.Tk()
root.withdraw()
app = gui_mod.LightstickPlayerApp(root)

# 程序化加载 CSV
demo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "examples", "demo.csv")
app.csv_var.set(demo)
app.load_csv()

assert len(app.frames) == 7, f"frames={len(app.frames)}"
print("加载帧数:", len(app.frames))

# 更新通道预览(模拟一帧)
app._update_channels([(0, 15, 0, 0)] * 9)
assert app.last_channels[0] == (0, 15, 0, 0)
print("通道预览更新 OK")

# 时间轴 total
print("总时长(ms):", app._total_ms())

root.update_idletasks()
root.destroy()
print("GUI 冒烟测试通过")
