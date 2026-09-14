# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""时间轴调度: 按 CSV 的 frame_time_ms 在正确时刻发帧。

主时钟可选:
  1) 视频主时钟(默认, 有 --video 时): 用 VlcPlayer.time_ms()
  2) 单调计时器(无视频): time.monotonic() 相对起点

限速(min_interval_ms): 每发一帧后, 至少间隔 min_interval_ms 才发下一帧。
用于 --throttle: 帧在时间轴上"等待"(延迟)而不是丢弃, 让发送节奏匹配固件的
处理速度(D8 一条命令空口约 786ms)。
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Tuple


class Timeline:
    def __init__(self, frames, send_fn: Callable, clock_ms: Optional[Callable[[], int]] = None,
                 min_interval_ms: int = 0, drop_late: bool = False):
        self.frames = frames
        self.send_fn = send_fn
        self._clock = clock_ms
        self._start = time.monotonic()
        self._next = 0
        self._done = False
        self._min_interval_ms = min_interval_ms
        # drop_late: 限速跟不上时直接跳过已过期的帧(保住与视频的同步),
        # 而不是排队等待(会越跑越落后)。
        self._drop_late = drop_late
        self.dropped = 0
        self._last_send_ms = -10**9   # 上次发送的时间点(时钟单位)
        self._next_ok_ms = -10**9     # 下一帧最早可发时间(时钟单位)

    def _now_ms(self) -> int:
        if self._clock is not None:
            t = self._clock()
            return int(t) if t and t > 0 else 0
        return int((time.monotonic() - self._start) * 1000)

    def tick(self) -> Tuple[int, bool]:
        """推进一个节拍, 发送所有已到时间(且满足限速)的帧。"""
        now = self._now_ms()
        sent = 0
        while self._next < len(self.frames) and self.frames[self._next].time_ms <= now:
            # 限速: 还没到下一帧允许发送的时间
            if self._min_interval_ms > 0 and now < self._next_ok_ms:
                if self._drop_late:
                    # 跟不上 -> 丢掉所有已经过期的帧, 从当前时刻重新对齐
                    while self._next < len(self.frames) and self.frames[self._next].time_ms <= now:
                        self._next += 1
                        self.dropped += 1
                break
            cost = self.send_fn(self.frames[self._next])   # send_fn 可返回本帧空口成本(ms)
            self._next += 1
            self._last_send_ms = now
            sent += 1
            interval = self._min_interval_ms
            if isinstance(cost, (int, float)) and int(cost) > interval:
                interval = int(cost)
            self._next_ok_ms = now + interval
        self._done = self._next >= len(self.frames)
        return sent, self._done

    @property
    def done(self) -> bool:
        return self._done

    @property
    def progress(self) -> int:
        return self._next
