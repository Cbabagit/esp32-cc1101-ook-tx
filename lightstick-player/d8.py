# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""向后兼容 shim: 协议已迁移到 protocol.py。

保留本文件以便旧代码 (import d8) 仍可用; 新代码请直接 import protocol。
"""
from __future__ import annotations

from protocol import (  # noqa: F401
    SLOT_WORDS,
    SLOT_ROTL,
    slot_word,
    d8_command as build_d8_command,
    build_d8_frame,
    checksum,
    rol16,
    PALETTE,
    STATE_NAMES,
    CMD_ZONE, CMD_10COL, CMD_PULSE, CMD_UNLOCK, CMD_SLOT,
    build_zone_frame, build_c0_frame, build_a6_frame, build_da_frame,
    zone_command, c0_command, a6_command, da_command,
    # 空口编码 / TX_PULSES
    AIR_PREFIX, D8_PHASES, D8_FRAME_INTERVAL_US, BIT_US,
    DEFAULT_FREQ_HZ, DEFAULT_POWER_DBM,
    frame_to_bits, bits_to_durations, build_d8_transaction,
    tx_pulses_command, d8_tx_command,
    FUNCTION_TO_STATE, MODE_INTERVAL_MS, nearest_palette,
    c0_tx_command, zone_tx_command,
    mode_interval, is_blackout,
    # 紧凑命令 (TX_D8 / TX_BYTES)
    D8_FRAME_BYTES, D8_SYMBOLS, AIR_GAP_US, MIN_BIT_US, MAX_FRAME_BYTES,
    frame_duration_us, tx_bytes_command, d8_slots, tx_d8_command,
    d8_air_time_us, tx_air_time_us,
)
