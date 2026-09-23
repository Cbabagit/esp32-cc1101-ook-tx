# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""视频: ffmpeg(ffprobe) 探测元数据 + python-vlc 播放并提供同步主时钟。

用法:
  probe_video(path) -> dict  用 ffprobe 读取时长/分辨率/帧率
  VlcPlayer(path)            python-vlc 播放, time_ms() 返回当前播放毫秒
  VlcPlayer.set_video_window(hwnd) / relocate(hwnd)
                             把画面嵌进 Tk 窗口, 或搬到全屏窗口后原地续播

注意: python-vlc 需要系统装有 VLC(libVLC); 未装时 import 会失败, 本模块
在真正创建 VlcPlayer 时才 import, 纯探测路径不依赖 VLC。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Optional


def _find(tool: str) -> str:
    p = shutil.which(tool)
    return p or tool


def probe_video(path: str) -> Dict[str, Any]:
    """用 ffprobe 返回视频元数据(duration_ms, fps, width, height)。"""
    cmd = [_find("ffprobe"), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("ffprobe 失败: " + out.stderr.strip())
    info = json.loads(out.stdout)

    fmt = info.get("format", {})
    duration_s = float(fmt.get("duration") or 0)

    vstream = next((s for s in info.get("streams", [])
                    if s.get("codec_type") == "video"), None)
    fps = 0.0
    if vstream:
        num, den = vstream.get("avg_frame_rate", "0/1").split("/")
        if float(den):
            fps = float(num) / float(den)

    return {
        "duration_ms": int(duration_s * 1000),
        "fps": fps,
        "width": (vstream or {}).get("width", 0),
        "height": (vstream or {}).get("height", 0),
        "codec": (vstream or {}).get("codec_name", ""),
    }


class VlcPlayer:
    """python-vlc 封装, 提供播放、毫秒级时钟, 以及画面窗口的搬家。

    "搬家" = 在嵌入式预览窗口与全屏窗口之间切换视频输出。libVLC 只在新建
    视频输出(vout)时才读取窗口句柄, 所以换窗口必须 stop -> 重新挂句柄 -> play,
    并记录/恢复播放位置, 否则时间轴主时钟会跳回 0 导致灯光重发。
    """

    def __init__(self, path: str, platform: Optional[str] = None):
        import vlc
        self._vlc = vlc
        self._platform = platform or sys.platform
        # libVLC 默认会把解码器/vout 的提示直接打到 stderr, 播放时刷屏,
        # 把真正有用的日志淹掉:
        #   avcodec decoder: Using D3D11VA ... for hardware decoding
        #   direct3d11 vout display error: SetThumbNailClip failed: 0x800706f4
        # 实测 --quiet 能把这两类都消掉 (9 行 -> 0 行)。
        # 排查播放问题时设 LIGHTSTICK_VLC_VERBOSE=1 恢复完整日志。
        verbose = os.environ.get("LIGHTSTICK_VLC_VERBOSE", "").strip().lower()
        quiet_args = [] if verbose not in ("", "0", "false", "no") else ["--quiet"]
        self.instance = vlc.Instance(quiet_args) if quiet_args else vlc.Instance()
        self.media = self.instance.media_new(path)
        self.player = self.instance.media_player_new()
        self.player.set_media(self.media)
        self._last_ms = 0
        self._seek_target = None    # 换窗口后等待 seek 回到的位置(ms)
        self._seek_arm_until = 0.0  # 这之前不接受"已到位", 见 time_ms()
        self._seek_deadline = 0.0
        self.fullscreen = False

    # ---- 播放控制 ----
    def play(self) -> None:
        self.player.play()

    def stop(self) -> None:
        self.player.stop()
        self._last_ms = 0
        self._seek_target = None

    def pause(self) -> None:
        self.player.pause()

    def resume(self) -> None:
        self.player.play()

    def is_playing(self) -> bool:
        return bool(self.player.is_playing())

    def is_ended(self) -> bool:
        try:
            return self.player.get_state() == self._vlc.State.Ended
        except Exception:
            return False

    def time_ms(self) -> int:
        """当前播放位置(ms), 保证单调不回退。

        两种情况会让 libVLC 报出比实际更早的位置:
          1) 换窗口刚 stop 完, get_time() 返回 -1;
          2) 换窗口后重新 play, 缓冲期间 get_time() 先回到 0 再 seek 回去。
        视频是灯光同步的主时钟, 这两种情况都必须挡住 —— 否则时间轴会以为
        时间倒流, 灯光要么停住要么重发。
        """
        raw = self.player.get_time()
        if raw is None or raw < 0:
            return self._last_ms
        raw = int(raw)
        if self._seek_target is not None:
            now = time.monotonic()
            # 刚 play() 完 get_time() 还可能停留在换窗口之前的位置, 那会让
            # "已到位"判断提前成立; 所以 arm 之后先静默一小段再认。
            if now >= self._seek_arm_until and raw + 100 >= self._seek_target:
                self._seek_target = None          # seek 已到位, 恢复真实时钟
            elif now < self._seek_deadline:
                return self._last_ms              # 缓冲中, 先沿用旧位置
            else:
                self._seek_target = None          # seek 没生效, 只能用真实值
        self._last_ms = raw
        return self._last_ms

    def set_time(self, ms: int) -> None:
        self.player.set_time(int(ms))
        self._last_ms = max(0, int(ms))

    def duration_ms(self) -> int:
        d = self.player.get_length()
        return int(d) if d and d > 0 else 0

    # ---- 窗口 ----
    def set_video_window(self, window_id: int) -> None:
        """把视频输出绑到一个原生窗口(Tk 的 widget.winfo_id())。"""
        if self._platform.startswith("win"):
            self.player.set_hwnd(window_id)
        elif self._platform == "darwin":
            self.player.set_nsobject(window_id)
        else:
            self.player.set_xwindow(window_id)

    def set_fullscreen(self, full: bool = False) -> None:
        """交给 libVLC 自己全屏(不做窗口搬家时用, 例如命令行模式)。"""
        self.fullscreen = bool(full)
        self.player.set_fullscreen(self.fullscreen)

    def is_fullscreen(self) -> bool:
        try:
            return bool(self.player.is_fullscreen())
        except Exception:
            return self.fullscreen

    def relocate(self, window_id: int) -> int:
        """换视频输出窗口并原地续播, 返回恢复到的毫秒位置。

        暂停中搬家会保持暂停; 已播到末尾则不自动重播。
        """
        position = max(0, self.time_ms())
        was_playing = self.is_playing()
        was_paused = bool(self.player.get_state() == self._vlc.State.Paused)
        active = was_playing or was_paused or position > 0
        self.player.stop()
        self.set_video_window(window_id)
        if active:
            self.player.play()
            if position > 0:
                self.player.set_time(position)
                self._seek_target = position
                self._seek_arm_until = time.monotonic() + 0.25
                self._seek_deadline = self._seek_arm_until + 5.0
            if was_paused and not was_playing:
                self.player.set_pause(1)
        self._last_ms = position
        return position