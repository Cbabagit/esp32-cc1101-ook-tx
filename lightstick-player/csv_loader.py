# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""lightstick-player CSV 解析。

CSV 表头列名:
  frame_time_ms, frame_id, frame_type, marker,
  ch0_function, ch0_red, ch0_green, ch0_blue, ... ch9_*

通道取值: function 0-3, red/green/blue 0-15。
本模块纯 Python, 无第三方依赖, 可直接单测。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class Frame:
    time_ms: float
    frame_id: int
    frame_type: str
    marker: str
    # 每个通道 (func, r, g, b), 已按 0-15 截断
    channels: List[Tuple[int, int, int, int]]


def _clamp4(v) -> int:
    try:
        return int(float(v)) & 0xF
    except (ValueError, TypeError):
        return 0


def _clamp_func(v) -> int:
    try:
        return int(float(v)) & 0x3   # 功能码 0-3
    except (ValueError, TypeError):
        return 0


def detect_channel_count(header: List[str]) -> int:
    n = 0
    while f"ch{n}_function" in header:
        n += 1
    return n


def load_csv(path: str) -> List[Frame]:
    frames: List[Frame] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        nch = detect_channel_count(header)
        if nch == 0:
            raise ValueError("CSV 缺少 ch0_function 列")
        for row in reader:
            chans: List[Tuple[int, int, int, int]] = []
            for i in range(nch):
                chans.append((
                    _clamp_func(row.get(f"ch{i}_function", "0")),
                    _clamp4(row.get(f"ch{i}_red", "0")),
                    _clamp4(row.get(f"ch{i}_green", "0")),
                    _clamp4(row.get(f"ch{i}_blue", "0")),
                ))
            frames.append(Frame(
                time_ms=float(row.get("frame_time_ms", 0) or 0),
                frame_id=int(row.get("frame_id", 0) or 0),
                frame_type=(row.get("frame_type") or "").strip(),
                marker=(row.get("marker") or "").strip(),
                channels=chans,
            ))
    frames.sort(key=lambda fr: fr.time_ms)
    return frames
