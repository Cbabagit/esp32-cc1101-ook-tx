# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
"""GUI 的 CPU 占用基准。

用 process_time() 只统计本进程的 CPU 时间, 不含别的程序; 不接串口 (省掉硬件变量)。
每次调用只跑一个场景, 便于外层用独立进程多次取中位数 —— 同进程连跑几个场景会互相
影响, 单次采样噪声能到 50%。

    py tools/bench_cpu.py --case c0       # 单个场景
    py tools/bench_cpu.py --case all      # 全部场景
    py tools/bench_cpu.py --case video --seconds 8
"""
import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk

import gui as gui_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = ("idle", "c0", "d8", "video")


def measure(setup, seconds):
    root = tk.Tk()
    root.geometry("900x600+50+50")
    app = gui_mod.LightstickPlayerApp(root)
    setup(app, root)

    state = {}

    def sample():
        if "t0" not in state:
            state["t0"] = time.process_time()
            state["w0"] = time.monotonic()
        elapsed = time.monotonic() - state["w0"]
        if elapsed >= seconds:
            state["cpu"] = time.process_time() - state["t0"]
            state["wall"] = elapsed
            root.quit()
            return
        root.after(50, sample)

    root.after(800, sample)      # 热身
    root.mainloop()
    root.destroy()
    return state.get("cpu", 0.0), state.get("wall", 1.0)


def load(app, name):
    app.csv_var.set(os.path.join(ROOT, "examples", name))
    app.load_csv()


def setup_for(case):
    if case == "idle":
        return lambda app, root: None

    def play(app, _root):
        app.mode_var.set("d8" if case == "d8" else "c0")
        if case == "d8":
            app.burst_var.set("1")
        if case == "video":
            app.video_var.set(os.path.join(ROOT, "examples", "testclip.mp4"))
        load(app, "fast60.csv")          # 60ms 一帧, 最坏情况
        app.play()

    return play


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=CASES + ("all",), default="all")
    parser.add_argument("--seconds", type=float, default=5.0)
    args = parser.parse_args()

    cases = CASES if args.case == "all" else (args.case,)
    for case in cases:
        cpu, wall = measure(setup_for(case), args.seconds)
        print("CPU %-6s %6.3f %5.2f" % (case, cpu, wall), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
