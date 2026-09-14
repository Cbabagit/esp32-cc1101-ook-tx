# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""lightstick-player 桌面 UI (Tkinter)。

复用本目录已有的 csv_loader / d8 / transport / video / timeline 模块,
提供: 加载 CSV + 视频 + 串口连接 + 播放/暂停/停止 + 同步偏移 + 10 通道实时
预览 + 时间轴进度 + 视频内嵌预览(python-vlc)。

运行: python gui.py
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import csv_loader
import d8
import main as main_mod
import timeline as timeline_mod
import transport as transport_mod
import video as video_mod

FUNC_NAME = {0: "常亮", 1: "1Hz", 2: "2Hz", 3: "4Hz"}


def rgb4_to_hex(r: int, g: int, b: int) -> str:
    return "#%02X%02X%02X" % (r * 17, g * 17, b * 17)


class PausableClock:
    """单调时钟, 支持暂停(用于无视频的 CSV 播放)。"""

    def __init__(self):
        self._start = time.monotonic()
        self._paused_at = None
        self._accum = 0.0

    def pause(self):
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def resume(self):
        if self._paused_at is not None:
            self._accum += time.monotonic() - self._paused_at
            self._paused_at = None

    def reset(self):
        self._start = time.monotonic()
        self._paused_at = None
        self._accum = 0.0

    def now_ms(self) -> int:
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return int((now - self._start - self._accum) * 1000)


class LightstickPlayerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("lightstick-player")
        root.geometry("1040x760")

        self.frames = []
        self.video_path = None
        self.tx = None
        self.vp = None
        self.tl = None
        self.sync_offset = 0
        self.clock = PausableClock()
        self.state = "idle"          # idle | playing | paused
        self.fs_window = None        # 全屏播放窗口(Toplevel)
        self.fs_holder = None        # 全屏窗口里承载视频输出的 Frame
        self.fs_active = False       # 当前视频是否输出到全屏窗口
        self.last_channels = [(0, 0, 0, 0)] * d8.SLOT_WORDS
        self.freq = d8.DEFAULT_FREQ_HZ
        self.power = d8.DEFAULT_POWER_DBM

        self._build_ui()
        self.refresh_ports()
        self._tick()

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        pad = dict(padx=6, pady=4)

        # 顶部: 文件 + 串口
        top = ttk.LabelFrame(self.root, text="文件与连接")
        top.pack(fill="x", **pad)

        row1 = ttk.Frame(top)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="CSV:").pack(side="left")
        self.csv_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.csv_var, width=46).pack(side="left", padx=4)
        ttk.Button(row1, text="浏览", command=self.pick_csv).pack(side="left")
        ttk.Button(row1, text="加载", command=self.load_csv).pack(side="left", padx=4)

        row2 = ttk.Frame(top)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="视频:").pack(side="left")
        self.video_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.video_var, width=46).pack(side="left", padx=4)
        ttk.Button(row2, text="浏览", command=self.pick_video).pack(side="left")

        row3 = ttk.Frame(top)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="串口:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(row3, textvariable=self.port_var, width=16)
        self.port_cb.pack(side="left", padx=4)
        ttk.Button(row3, text="刷新", command=self.refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(row3, text="连接", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=4)
        self.conn_state = ttk.Label(row3, text="未连接")
        self.conn_state.pack(side="left", padx=6)

        # 播放控制
        ctl = ttk.LabelFrame(self.root, text="播放控制")
        ctl.pack(fill="x", **pad)
        ctr = ttk.Frame(ctl)
        ctr.pack(fill="x", **pad)
        self.play_btn = ttk.Button(ctr, text="▶ 播放", command=self.play)
        self.play_btn.pack(side="left")
        self.pause_btn = ttk.Button(ctr, text="⏸ 暂停", command=self.pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctr, text="⏹ 停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.fs_btn = ttk.Button(ctr, text="⛶ 全屏", command=self.toggle_fullscreen,
                                 state="disabled")
        self.fs_btn.pack(side="left", padx=(8, 0))
        self.fs_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctr, text="播放时全屏",
                        variable=self.fs_auto_var).pack(side="left", padx=(6, 0))

        ttk.Label(ctr, text="灯光延迟(ms):").pack(side="left", padx=(18, 2))
        self.offset_var = tk.IntVar(value=0)
        ttk.Spinbox(ctr, from_=-10000, to=10000, textvariable=self.offset_var,
                    width=8, command=self._on_offset).pack(side="left")
        ttk.Label(ctr, text="(正=灯光滞后视频)").pack(side="left", padx=4)

        # 第二行: 协议模式 + 空口参数 (决定每帧占用的空口时长)
        par = ttk.Frame(ctl)
        par.pack(fill="x", **pad)
        ttk.Label(par, text="模式:").pack(side="left")
        self.mode_var = tk.StringVar(value="d8")
        ttk.Combobox(par, textvariable=self.mode_var, values=("d8", "c0", "zone"),
                     width=6, state="readonly").pack(side="left")
        ttk.Label(par, text="D8爆发:").pack(side="left", padx=(10, 2))
        self.burst_var = tk.StringVar(value="1")
        ttk.Combobox(par, textvariable=self.burst_var, values=("1", "2", "3", "6"),
                     width=3, state="readonly").pack(side="left")
        ttk.Label(par, text="重复(c0/zone):").pack(side="left", padx=(10, 2))
        self.repeat_var = tk.StringVar(value="1")
        ttk.Combobox(par, textvariable=self.repeat_var, values=("1", "2", "3", "6"),
                     width=3, state="readonly").pack(side="left")
        ttk.Label(par, text="符号宽度(us):").pack(side="left", padx=(10, 2))
        self.bit_us_var = tk.StringVar(value=str(d8.BIT_US))
        ttk.Combobox(par, textvariable=self.bit_us_var,
                     values=("250", "225", "200", "190", "150"),
                     width=5).pack(side="left")
        ttk.Label(par, text=f"(实测下限 {d8.MIN_BIT_US})").pack(side="left", padx=4)
        self.throttle_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(par, text="限速", variable=self.throttle_var).pack(side="left", padx=(10, 2))
        self.drop_late_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(par, text="丢帧保同步", variable=self.drop_late_var).pack(side="left", padx=2)
        self.mode_est = ttk.Label(par, text="")
        self.mode_est.pack(side="left", padx=10)
        self.mode_var.trace_add("write", lambda *_: self._refresh_estimate())
        self.burst_var.trace_add("write", lambda *_: self._refresh_estimate())
        self.repeat_var.trace_add("write", lambda *_: self._refresh_estimate())
        self.bit_us_var.trace_add("write", lambda *_: self._refresh_estimate())
        self._refresh_estimate()

        # 时间轴进度
        tlrow = ttk.Frame(ctl)
        tlrow.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(tlrow, orient="horizontal", mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.time_label = ttk.Label(tlrow, text="0.0s / 0.0s", width=18)
        self.time_label.pack(side="left", padx=6)

        # 通道预览
        ch = ttk.LabelFrame(self.root, text="通道预览 (实时)")
        ch.pack(fill="x", **pad)
        self.ch_grid = ttk.Frame(ch)
        self.ch_grid.pack(fill="x", **pad)
        self.swatches = []
        self.func_labels = []
        self.rgb_labels = []
        for i in range(d8.SLOT_WORDS):
            self.swatches.append(self._channel_cell(self.ch_grid, i))

        # 视频预览
        vf = ttk.LabelFrame(self.root, text="视频预览")
        vf.pack(fill="both", expand=True, **pad)
        self.video_frame = tk.Frame(vf, bg="black")
        self.video_frame.pack(fill="both", expand=True, padx=2, pady=2)
        self.video_placeholder = tk.Label(self.video_frame, text="未加载视频", bg="black", fg="#666")
        self.video_placeholder.pack(expand=True)

        # 全屏快捷键: 主窗口 F11 切换, Esc 退出; 双击预览画面也能切换
        self.root.bind("<F11>", lambda _e: self.toggle_fullscreen())
        self.root.bind("<Escape>", lambda _e: self._exit_fullscreen())
        self.video_frame.bind("<Double-Button-1>", lambda _e: self.toggle_fullscreen())
        self.video_placeholder.bind("<Double-Button-1>", lambda _e: self.toggle_fullscreen())

        # 日志
        lg = ttk.LabelFrame(self.root, text="日志")
        lg.pack(fill="x", **pad)
        self.log = tk.Text(lg, height=6, state="disabled")
        self.log.pack(fill="both", expand=True, padx=2, pady=2)

    def _channel_cell(self, parent, i):
        cell = ttk.Frame(parent)
        cell.pack(side="left", expand=True, fill="x", padx=2)
        sw = tk.Label(cell, text=f"ch{i}", bg="#000000", fg="white", height=3, width=8)
        sw.pack(fill="x")
        fn = ttk.Label(cell, text="常亮", anchor="center")
        fn.pack(fill="x")
        rgb = ttk.Label(cell, text="0,0,0", anchor="center", font=("Consolas", 8))
        rgb.pack(fill="x")
        return (sw, fn, rgb)

    # ---------------- 日志 ----------------
    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        # 限制行数, 防止日志无限增长拖慢 GUI
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 500:
            self.log.delete("1.0", f"{line_count - 500}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 文件/串口 ----------------
    def pick_csv(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")])
        if p:
            self.csv_var.set(p)

    def pick_video(self):
        p = filedialog.askopenfilename(filetypes=[("视频", "*.mp4 *.mkv *.avi *.mov *.webm"), ("所有文件", "*.*")])
        if p:
            self.video_var.set(p)
            self.video_path = p

    def load_csv(self):
        path = self.csv_var.get().strip()
        if not path:
            messagebox.showwarning("lightstick-player", "请先选择 CSV 文件")
            return
        try:
            self.frames = csv_loader.load_csv(path)
            nch = len(self.frames[0].channels) if self.frames else 0
            self._log(f"加载 {len(self.frames)} 帧, {nch} 通道: {path}")
        except Exception as e:
            messagebox.showerror("lightstick-player", f"CSV 解析失败: {e}")

    def refresh_ports(self):
        ports = transport_mod.list_ports()
        self.port_cb["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def toggle_connect(self):
        if self.tx is not None:
            self.tx.close()
            self.tx = None
            self.connect_btn.config(text="连接")
            self.conn_state.config(text="未连接")
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("lightstick-player", "请选择串口")
            return
        try:
            self.tx = transport_mod.LightstickTransport(port, 921600, log=self._log)
            self.connect_btn.config(text="断开")
            self.conn_state.config(text=f"已连接 {port}")
            self._log(f"串口已连接: {port}")
        except Exception as e:
            messagebox.showerror("lightstick-player", f"串口打开失败: {e}")

    # ---------------- 播放控制 ----------------
    def _params(self):
        """从界面读出本次播放的空口参数, 非法值回落到默认。"""
        try:
            burst = int(self.burst_var.get())
        except ValueError:
            burst = 6
        try:
            repeat = int(self.repeat_var.get())
        except ValueError:
            repeat = 1
        try:
            bit_us = int(self.bit_us_var.get())
        except ValueError:
            bit_us = d8.BIT_US
        return self.mode_var.get(), burst, repeat, max(50, min(2000, bit_us))

    def _refresh_estimate(self):
        mode, burst, repeat, bit_us = self._params()
        try:
            cost = main_mod.frames_cost_ms(mode, burst, bit_us, d8.AIR_GAP_US, repeat, True)
        except Exception:
            cost = 0
        self.mode_est.config(text=f"每帧约 {cost}ms")

    def _on_offset(self):
        self.sync_offset = self.offset_var.get()

    def _clock(self):
        if self.vp is not None:
            return lambda: max(0, self.vp.time_ms() - self.sync_offset)
        return lambda: max(0, self.clock.now_ms() - self.sync_offset)

    def play(self):
        if not self.frames:
            messagebox.showwarning("lightstick-player", "请先加载 CSV")
            return
        if self.tx is None:
            # 未连接串口也可预览(无输出)
            self._log("警告: 未连接串口, 仅本地预览")

        # 视频
        self.video_path = self.video_var.get().strip() or None
        if self.video_path:
            try:
                self.vp = video_mod.VlcPlayer(self.video_path)
                # 勾了"播放时全屏"就先建全屏窗口, 视频直接起在全屏上
                if self.fs_auto_var.get():
                    self._open_fullscreen_window()
                self._attach_video()
                self.vp.play()
                if self.fs_active:
                    self.fs_window.focus_force()
            except Exception as e:
                messagebox.showwarning("lightstick-player", f"VLC 初始化失败(未装 VLC?): {e}")
                self._close_fullscreen_window()
                self.vp = None

        # 时间轴
        self.clock.reset()
        sender = self._make_sender()
        mode, burst, repeat, bit_us = self._params()
        cost = main_mod.frames_cost_ms(mode, burst, bit_us, d8.AIR_GAP_US, repeat, True)
        min_interval = cost if self.throttle_var.get() else 0
        drop_late = self.drop_late_var.get()
        if self.throttle_var.get():
            self._log(f"限速 {min_interval}ms/帧 ({mode}, 符号 {bit_us}us, 重复 {repeat})"
                      + ("  [丢帧保同步]" if drop_late else "  [排队等待, 会逐渐落后]"))
        self.tl = timeline_mod.Timeline(self.frames, sender, clock_ms=self._clock(),
                                        min_interval_ms=min_interval, drop_late=drop_late)
        self.state = "playing"
        self.play_btn.config(state="disabled")
        self.pause_btn.config(state="normal")
        self.stop_btn.config(state="normal")
        self.fs_btn.config(state="normal" if self.vp is not None else "disabled")
        self._log("开始播放")

    def pause(self):
        if self.state == "playing":
            if self.vp is not None:
                self.vp.player.pause()
            else:
                self.clock.pause()
            self.state = "paused"
            self.pause_btn.config(text="▶ 继续")
            self._log("暂停")
        elif self.state == "paused":
            if self.vp is not None:
                self.vp.player.pause()
            else:
                self.clock.resume()
            self.state = "playing"
            self.pause_btn.config(text="⏸ 暂停")
            self._log("继续")

    def stop(self):
        self._close_fullscreen_window()
        if self.vp is not None:
            try:
                self.vp.stop()
                self.vp.player.release()
            except Exception:
                pass
            self.vp = None
        self.tl = None
        self.clock.reset()
        self.state = "idle"
        self.play_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="⏸ 暂停")
        self.stop_btn.config(state="disabled")
        self.fs_btn.config(state="disabled", text="⛶ 全屏")
        self.progress["value"] = 0
        self.time_label.config(text="0.0s / 0.0s")
        self.video_placeholder.pack(expand=True)
        self._log("停止")

    # ---------------- 全屏播放 ----------------
    def _video_target(self):
        """当前应该承载视频输出的 Tk 窗口部件(全屏窗口优先)。"""
        if self.fs_active and self.fs_holder is not None:
            return self.fs_holder
        return self.video_frame

    def _attach_video(self):
        """开播前把视频输出绑到目标窗口。"""
        if self.vp is None:
            return
        target = self._video_target()
        if target is self.video_frame:
            self.video_placeholder.pack_forget()
        # libVLC 要在窗口已经真正创建/映射之后才能建立视频输出, 只做
        # update_idletasks() 不够(那时 HWND 还没映射, vout 会创建失败)。
        try:
            if not self.root.winfo_viewable():
                self.root.deiconify()
            self.root.update()
            if self.fs_window is not None:
                self.fs_window.update()
        except Exception:
            pass
        try:
            self.vp.set_video_window(target.winfo_id())
        except Exception as e:
            self._log(f"视频输出绑定失败: {e}")

    def _embed_video(self):
        self._attach_video()

    def toggle_fullscreen(self):
        """在全屏窗口与嵌入式预览之间切换视频输出(播放不中断)。"""
        if self.fs_active:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if self.vp is None:
            messagebox.showinfo("lightstick-player", "请先加载视频并开始播放")
            return
        if self.fs_active:
            return
        if self._open_fullscreen_window() is None:
            return
        try:
            self.vp.relocate(self.fs_holder.winfo_id())
        except Exception as e:
            self._log(f"全屏切换失败: {e}")
            self._close_fullscreen_window()
            return
        self.fs_window.focus_force()
        self.fs_btn.config(text="⛶ 退出全屏")
        self._log("已进入视频全屏 (Esc / F11 退出)")

    def _open_fullscreen_window(self):
        """建好全屏 Toplevel 并把 fs_active 置位(不搬视频输出)。"""
        if self.fs_active:
            return self.fs_window
        win = tk.Toplevel(self.root)
        win.title("lightstick-player 全屏 (Esc 退出)")
        win.configure(bg="black")
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        holder = tk.Frame(win, bg="black", highlightthickness=0, bd=0)
        holder.pack(fill="both", expand=True)
        win.update()          # 必须真正映射, 否则 libVLC 建不出视频输出
        # Esc / F11 / 关闭窗口 都退回嵌入预览
        win.bind("<Escape>", lambda _e: self._exit_fullscreen())
        win.bind("<F11>", lambda _e: self._exit_fullscreen())
        win.protocol("WM_DELETE_WINDOW", self._exit_fullscreen)
        self.fs_window = win
        self.fs_holder = holder
        self.fs_active = True
        return win

    def _close_fullscreen_window(self):
        """只销毁全屏窗口, 不动视频输出(用于停止/初始化失败等收尾)。"""
        win, self.fs_window = self.fs_window, None
        self.fs_holder = None
        self.fs_active = False
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        try:
            self.fs_btn.config(text="⛶ 全屏")
        except Exception:
            pass

    def _exit_fullscreen(self):
        if not self.fs_active:
            return
        # 先把画面搬回嵌入预览, 再销毁全屏窗口(否则 hwnd 变野指针)
        embedded = False
        if self.vp is not None:
            self.fs_active = False          # 让 _attach_video 认准嵌入预览
            self.root.update()
            try:
                self.vp.relocate(self.video_frame.winfo_id())
                embedded = True
            except Exception as e:
                self._log(f"退出全屏失败: {e}")
            self.fs_active = True
        self._close_fullscreen_window()
        if embedded:
            self.video_placeholder.pack_forget()
        self._log("已退出全屏")

    # ---------------- 发帧 + 预览 ----------------
    def _make_sender(self):
        seq = [0]
        mode, burst, repeat, bit_us = self._params()

        def send(frame):
            cmds, cost = main_mod.commands_for_frame(
                frame, mode, self.freq, self.power, burst, False,
                bit_us=bit_us, gap_us=d8.AIR_GAP_US, repeat=repeat, compact=True)
            for cmd in cmds:
                seq[0] += 1
                cmd["id"] = str(seq[0])
                if self.tx is not None:
                    self.tx.send_json(cmd)
            self._update_channels(frame.channels[:d8.SLOT_WORDS])
            self._log(f"t={frame.time_ms:.0f}ms  {mode.upper()} TX #{seq[0]} ({len(cmds)} cmd, {cost}ms)")
            return cost   # 本帧空口成本, 供时间轴限速使用
        return send

    def _update_channels(self, chans):
        self.last_channels = chans
        for i, (sw, fn, rgb) in enumerate(self.swatches):
            f, r, g, b = chans[i] if i < len(chans) else (0, 0, 0, 0)
            sw.config(bg=rgb4_to_hex(r, g, b))
            fn.config(text=FUNC_NAME.get(f & 0x3, str(f)))
            rgb.config(text=f"{r},{g},{b}")

    # ---------------- 主循环 tick ----------------
    def _tick(self):
        if self.tl is not None and self.state in ("playing", "paused"):
            if self.state == "playing":
                self.tl.tick()
            now = self._clock()()
            total = self._total_ms()
            if total > 0:
                self.progress["maximum"] = total
                self.progress["value"] = min(now, total)
            self.time_label.config(text=f"{now/1000:.1f}s / {total/1000:.1f}s")
            if self.tl.done and (self.vp is None or not self.vp.is_playing()):
                if self.tl.dropped:
                    self._log(f"播放完成 (跳过 {self.tl.dropped} 帧以跟上视频)")
                else:
                    self._log("播放完成")
                self.stop()
        self.root.after(5, self._tick)

    def _total_ms(self):
        total = 0
        if self.frames:
            total = int(self.frames[-1].time_ms)
        if self.vp is not None:
            total = max(total, self.vp.duration_ms())
        return total


def sys_platform():
    import sys
    return sys.platform


def main():
    root = tk.Tk()
    LightstickPlayerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
