# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""全屏播放真机测试: 真起 VLC 播一段视频, 验证嵌入<->全屏切换时画面不中断、
主时钟不倒退、CSV 帧继续按时间轴下发。

需要: 已安装 VLC(libvlc) + examples/testclip.mp4 (没有就用 ffmpeg 生成)。
运行: python tests/test_fullscreen.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk

import gui as gui_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


def pump(root, seconds):
    """不用 mainloop: 手动跑 Tk 事件循环, 让界面与视频持续刷新。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def wait_clock(app, root, target_ms=300, timeout=10.0):
    """libVLC 起播后要先缓冲约 1 秒 get_time() 才开始走, 等到真正推进为止。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if app.vp is not None and app.vp.time_ms() >= target_ms:
            return app.vp.time_ms()
        time.sleep(0.02)
    return app.vp.time_ms() if app.vp is not None else 0


def main():
    clip = os.path.join(ROOT, "examples", "testclip.mp4")
    if not os.path.exists(clip):
        print("缺少 examples/testclip.mp4, 先用 ffmpeg 生成:")
        print('  ffmpeg -y -f lavfi -i "testsrc=size=1280x720:rate=30" -t 8 '
              "-pix_fmt yuv420p examples/testclip.mp4")
        return 1

    root = tk.Tk()
    app = gui_mod.LightstickPlayerApp(root)
    app.csv_var.set(os.path.join(ROOT, "examples", "fast60.csv"))
    app.load_csv()
    app.video_var.set(clip)
    app.delay_ms = lambda *a, **k: None
    print("加载:", len(app.frames), "帧 +", os.path.basename(clip))

    try:
        app.play()
        check(app.vp is not None, "VLC 播放器已创建")

        for _ in range(300):
            root.update()
            if app.vp is not None and app.vp.is_playing():
                break
            time.sleep(0.02)
        check(app.vp is not None and app.vp.is_playing(), "视频已开始播放")

        t_embed = wait_clock(app, root)
        check(t_embed >= 300, f"嵌入预览时主时钟在走 (t={t_embed}ms)")

        # --- 进入全屏 ---
        app.toggle_fullscreen()
        check(app.fs_active and app.fs_window is not None, "已进入全屏窗口")
        check(app.fs_btn.cget("text").endswith("退出全屏"), "按钮已切到退出全屏")
        pump(root, 0.3)
        t_fs_0 = app.vp.time_ms()
        t_fs = wait_clock(app, root, target_ms=t_fs_0 + 300)
        check(app.vp.is_playing(), "全屏后仍在播放")
        check(t_fs_0 >= t_embed, f"全屏切换没有把主时钟带回去 ({t_embed} -> {t_fs_0})")
        check(t_fs - t_fs_0 >= 300, f"全屏期间时钟继续推进 (delta={t_fs - t_fs_0}ms)")
        frames_at_fs = app.tl.progress
        check(frames_at_fs > 0, f"全屏期间 CSV 帧继续下发 ({frames_at_fs} 帧)")

        # --- 退出全屏 ---
        app.toggle_fullscreen()
        check(not app.fs_active and app.fs_window is None, "已退出全屏并销毁窗口")
        pump(root, 0.3)
        t_back_0 = app.vp.time_ms()
        t_back = wait_clock(app, root, target_ms=t_back_0 + 300)
        check(app.vp.is_playing(), "退回嵌入预览后仍在播放")
        check(t_back_0 >= t_fs, f"退回嵌入预览没有把主时钟带回去 ({t_fs} -> {t_back_0})")
        check(t_back - t_back_0 >= 300, f"退回后时钟继续推进 (delta={t_back - t_back_0}ms)")
        check(app.tl.progress > frames_at_fs,
              f"退回后 CSV 帧继续下发 ({frames_at_fs} -> {app.tl.progress})")

        # --- 播放中全屏停止, 收尾必须干净 ---
        app.toggle_fullscreen()
        pump(root, 0.3)
        app.stop()
        check(app.fs_window is None and not app.fs_active, "停止时全屏窗口被清掉")
        check(app.vp is None and app.tl is None, "停止后播放器与时间轴已释放")
        check(str(app.fs_btn.cget("state")) == "disabled",
              f"停止后全屏按钮禁用 (state={app.fs_btn.cget('state')!r})")
    finally:
        try:
            app._close_fullscreen_window()
            if app.vp is not None:
                app.vp.stop()
        except Exception:
            pass
        root.update_idletasks()
        root.destroy()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
