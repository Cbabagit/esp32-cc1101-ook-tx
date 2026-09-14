# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""lightstick-player 协议层: 空口帧构造与下发 (协议实现全在主机侧)。

依据 Lightstick-Lab 的权威协议文档 (docs/2511t-v2-air-protocol-control.md) 补齐:
  00 分区命令 / C0 十区独立色 / A6 短脉冲 / DA 按键解锁 / D8 9槽 RGB。

物理层: 433.920 MHz ASK/OOK, 每符号 250us; 前导 11111111000011110 + 每数据位
d 展开为 0,1,d (MSB-first); 校验 = (0x96 + 前面所有帧字节之和) & 0xFF。

本模块纯 Python, 无第三方依赖, 可直接单测。
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

# ---- 常量 (与固件 protocol.h 一致) ----
CHECKSUM_SEED = 0x96
SLOT_WORDS = 9
SLOT_ROTL = 2

CMD_ZONE = 0x00    # 分区颜色/状态 (7 字节)
CMD_10COL = 0xC0   # A-J 十区独立 4bit 色码 (7 字节)
CMD_PULSE = 0xA6   # 全局短脉冲 (3 字节)
CMD_UNLOCK = 0xDA  # 实体按键解锁 (7 字节)
CMD_SLOT = 0xD8    # 9 槽 RGB (21 字节)

# 00 命令状态码 S
STATE_NAMES = {
    0x00: "灭", 0x01: "常亮", 0x02: "慢闪", 0x03: "中闪", 0x04: "快闪",
    0x05: "Fade in", 0x06: "Fade out", 0x0B: "保持状态",
}

# 16 色调色板: 色码 -> (名称, 理想 RGB hex)
PALETTE = {
    0x00: ("红", "#FF0000"), 0x01: ("绿", "#00B51A"), 0x02: ("蓝", "#1878FF"),
    0x03: ("粉", "#FF007C"), 0x04: ("白", "#FFFFFF"), 0x05: ("黄", "#FFD400"),
    0x06: ("浅蓝", "#66CCFF"), 0x07: ("浅绿", "#00D878"), 0x08: ("紫", "#8A4DFF"),
    0x09: ("橙", "#FF6A00"), 0x0A: ("浅粉", "#FF8AE0"), 0x0B: ("较深蓝", "#1B90FF"),
    0x0C: ("浅黄", "#FFF29A"), 0x0D: ("较深绿", "#007B66"), 0x0E: ("浅红", "#FF5C5C"),
    0x0F: ("更亮白", "#F8FAFF"),
}
COLOR_KEEP = 0xAA  # 保持当前颜色


# ---- 纯助手 ----
def checksum(data: bytes) -> int:
    return (CHECKSUM_SEED + sum(data)) & 0xFF


def rol16(v: int, n: int) -> int:
    n %= 16
    return ((v << n) | (v >> (16 - n))) & 0xFFFF


def slot_word(func: int, r: int, g: int, b: int) -> int:
    """打包逻辑 slot word: FFFF RRRR GGGG BBBB。"""
    return ((func & 0xF) << 12) | ((r & 0xF) << 8) | ((g & 0xF) << 4) | (b & 0xF)


# ---- 原始帧构造 (含校验, 用于金标验证) ----
def build_zone_frame(m1: int, m2: int, x: int = 0xFF, state: int = 0x01, color: int = 0x00) -> bytes:
    body = bytes([CMD_ZONE, m1 & 0xFF, m2 & 0xFF, x & 0xFF, state & 0xFF, color & 0xFF])
    return body + bytes([checksum(body)])


def build_c0_frame(colors: Sequence[int]) -> bytes:
    """C0 AB CD EF GH IJ K — colors[0..9] 依次 A..J 的 4bit 色码。"""
    if len(colors) != 10:
        raise ValueError("C0 requires exactly 10 colors for A-J")
    packed = [((colors[i] & 0xF) << 4) | (colors[i + 1] & 0xF) for i in range(0, 10, 2)]
    body = bytes([CMD_10COL, *packed])
    return body + bytes([checksum(body)])


def build_a6_frame(color: int = 0x07) -> bytes:
    body = bytes([CMD_PULSE, color & 0x0F])
    return body + bytes([checksum(body)])


def build_da_frame(m1: int, m2: int, x1: int = 0xFF, x2: int = 0x01, x3: int = 0x00) -> bytes:
    body = bytes([CMD_UNLOCK, m1 & 0xFF, m2 & 0xFF, x1 & 0xFF, x2 & 0xFF, x3 & 0xFF])
    return body + bytes([checksum(body)])


def build_d8_frame(slots: Sequence[int], phase: int = 2) -> bytes:
    """D8 21 字节帧: D8 + 9 槽(ROL16<<2 大端) + phase + checksum。"""
    if len(slots) != SLOT_WORDS:
        raise ValueError(f"D8 requires exactly {SLOT_WORDS} slot words")
    body = bytearray([CMD_SLOT])
    for s in slots:
        w = rol16(s, SLOT_ROTL)
        body += bytes([(w >> 8) & 0xFF, w & 0xFF])
    body.append(phase & 0xFF)
    data = bytes(body)
    return data + bytes([checksum(data)])


# ---- 串口 CLI 命令串 (固件自行算校验/ROL16/爆发) ----
def d8_command(channels: List[Tuple[int, int, int, int]], max_channels: int = SLOT_WORDS) -> str:
    """'d8 <9个4位hex字>'。channels: [(func,r,g,b), ...]。"""
    words: List[int] = []
    for i in range(max_channels):
        if i < len(channels):
            f, r, g, b = channels[i]
            words.append(slot_word(f, r, g, b))
        else:
            words.append(0x0000)
    return "d8 " + " ".join(f"{w:04X}" for w in words)


def zone_command(m1: int, m2: int, s: int, c: int) -> str:
    """'zone <M1 M2 S C>'(4 个 hex 字节, X 默认 FF)。"""
    return "zone " + " ".join(f"{b:02X}" for b in (m1 & 0xFF, m2 & 0xFF, s & 0xFF, c & 0xFF))


def c0_command(colors: Sequence[int]) -> str:
    """'c0 <10个色码>'(A-J)。"""
    if len(colors) != 10:
        raise ValueError("c0 requires exactly 10 colors for A-J")
    return "c0 " + " ".join(f"{c & 0xF:X}" for c in colors)


def a6_command(color: int = 0x07) -> str:
    return f"a6 {color & 0xF:X}"


def da_command(m1: int = 0xFF, m2: int = 0xFF) -> str:
    return "da " + " ".join(f"{b:02X}" for b in (m1 & 0xFF, m2 & 0xFF))


# 向后兼容别名 (旧代码 import d8 时使用)
build_d8_command = d8_command


# ============================================================================
# 空口编码 (转成 Lightstick-Lab 参考固件的 TX_PULSES durations_us)
# 参考固件(ESP32-S3 + CC1101)通过串口接收 JSON 命令: {"id","cmd","args"}。
# D8 命令 = 6 帧爆发(phase 2,1,0,2,1,0), 帧间 850us 低电平, 合一个 durations_us。
# ============================================================================
AIR_PREFIX = "11111111000011110"     # 17-bit 前导
D8_PHASES = (2, 1, 0, 2, 1, 0)
D8_FRAME_INTERVAL_US = 131100         # 帧起始间隔
BIT_US = 250
DEFAULT_FREQ_HZ = 433_920_000
DEFAULT_POWER_DBM = -20


def frame_to_bits(frame: bytes) -> str:
    """帧字节 -> 空中 bit 串: 前导 17bit + 每数据位 0,1,d (MSB first)。"""
    bits = AIR_PREFIX
    for byte in frame:
        for i in range(7, -1, -1):
            d = (byte >> i) & 1
            bits += "01" + str(d)
    return bits


def bits_to_durations(bits: str, bit_us: int = BIT_US) -> List[int]:
    """bit 串 -> 游程编码的交替 duration 列表 (起始电平=1)。"""
    durations: List[int] = []
    current = bits[0]
    run = 0
    for b in bits:
        if b == current:
            run += 1
        else:
            durations.append(run * bit_us)
            current = b
            run = 1
    durations.append(run * bit_us)
    return durations


def build_d8_transaction(slots: Sequence[int], bit_us: int = BIT_US,
                         burst_frames: int = 6) -> List[int]:
    """9 个逻辑 slot -> D8 爆发的 durations_us (起始电平=1)。

    burst_frames: 6 = 两个完整 phase 周期(默认, 最可靠, 空口 786ms)
                  3 = 一个 2,1,0 周期(快一倍, 空口 393ms)
                  1 = 单帧(最快, 131ms; 固定 phase 可能被棒当重复帧)
    """
    n = max(1, min(int(burst_frames), len(D8_PHASES)))
    phases = tuple(D8_PHASES[i % len(D8_PHASES)] for i in range(n))
    merged: List[int] = []
    for i, phase in enumerate(phases):
        frame = build_d8_frame(slots, phase)
        durations = bits_to_durations(frame_to_bits(frame), bit_us)
        if i > 0:
            gap = D8_FRAME_INTERVAL_US - sum(durations)
            if len(merged) % 2 == 1:      # 上一帧结束于高电平 -> 追加一段低电平
                merged.append(gap)
            else:                          # 上一帧结束于低电平 -> 延长低电平
                merged[-1] += gap
        merged.extend(durations)
    return merged


def tx_pulses_command(durations_us: Sequence[int], request_id: str = "1",
                      frequency_hz: int = DEFAULT_FREQ_HZ,
                      power_dbm: int = DEFAULT_POWER_DBM,
                      repeat: int = 1, gap_us: int = 20000) -> dict:
    """构造参考固件的 TX_PULSES JSON 命令。"""
    return {
        "id": request_id,
        "cmd": "TX_PULSES",
        "args": {
            "durations_us": list(durations_us),
            "start_level": 1,
            "frequency_hz": frequency_hz,
            "power_dbm": power_dbm,
            "repeat": repeat,
            "gap_us": gap_us,
        },
    }


# ---- 00 状态码映射 (D8 function -> 00 state) ----
# D8 function: 0=常亮 1=0.89Hz 2=3.08Hz 3=6.10Hz
# 00 state  : 1=常亮 2=慢闪 3=中闪 4=快闪  (0=暗)
FUNCTION_TO_STATE = {0: 0x01, 1: 0x02, 2: 0x03, 3: 0x04}

# 各模式的一条命令空口时长(用于 --throttle 限速): d8=6帧爆发, c0/zone=单帧重复6次
MODE_INTERVAL_MS = {"d8": D8_FRAME_INTERVAL_US * 6 // 1000, "c0": 380, "zone": 380}


def nearest_palette(r: int, g: int, b: int) -> int:
    """RGB (0-15 每分量) -> 最近的 16 色调色板色码 (0-15)。"""
    r8, g8, b8 = r * 17, g * 17, b * 17
    best, best_dist = 0, 1 << 30
    for code, (_name, hx) in PALETTE.items():
        hr = int(hx[1:3], 16)
        hg = int(hx[3:5], 16)
        hb = int(hx[5:7], 16)
        d = (r8 - hr) ** 2 + (g8 - hg) ** 2 + (b8 - hb) ** 2
        if d < best_dist:
            best_dist, best = d, code
    return best


def _short_tx(frame: bytes, request_id: str, frequency_hz: int, power_dbm: int, repeat: int) -> dict:
    """单帧字节 -> TX_PULSES 命令(repeat 次重复, 帧间 20ms 低电平)。"""
    durations = bits_to_durations(frame_to_bits(frame))
    return tx_pulses_command(durations, request_id=request_id, frequency_hz=frequency_hz,
                             power_dbm=power_dbm, repeat=repeat, gap_us=20000)


def c0_tx_command(colors10: Sequence[int], request_id: str = "1",
                  frequency_hz: int = DEFAULT_FREQ_HZ,
                  power_dbm: int = DEFAULT_POWER_DBM, repeat: int = 6,
                  compact: bool = False, bit_us: int = BIT_US,
                  gap_us: int = 20000) -> dict:
    """10 个色码(A-J) -> C0 命令(单帧重复 repeat 次)。

    compact=True 走 TX_BYTES: 7 字节帧只发 14 个 hex 字符, 空口 185 符号
    (250us 时 46ms), 是能进 100ms 帧间隔的关键; 旧路径 6 次重复要 276ms。
    """
    frame = build_c0_frame(colors10)
    if compact:
        return tx_bytes_command(frame, request_id, frequency_hz, power_dbm,
                                repeat, bit_us, gap_us)
    return _short_tx(frame, request_id, frequency_hz, power_dbm, repeat)


def zone_tx_command(m1: int, m2: int, state: int, color: int, request_id: str = "1",
                    frequency_hz: int = DEFAULT_FREQ_HZ,
                    power_dbm: int = DEFAULT_POWER_DBM, repeat: int = 6,
                    compact: bool = False, bit_us: int = BIT_US,
                    gap_us: int = 20000) -> dict:
    """00 分区命令 -> 命令(单帧重复 repeat 次); compact=True 走 TX_BYTES。"""
    frame = build_zone_frame(m1, m2, 0xFF, state, color)
    if compact:
        return tx_bytes_command(frame, request_id, frequency_hz, power_dbm,
                                repeat, bit_us, gap_us)
    return _short_tx(frame, request_id, frequency_hz, power_dbm, repeat)


def d8_tx_command(channels: List[Tuple[int, int, int, int]], request_id: str = "1",
                  frequency_hz: int = DEFAULT_FREQ_HZ,
                  power_dbm: int = DEFAULT_POWER_DBM,
                  burst_frames: int = 6) -> dict:
    """CSV 通道 (func,r,g,b) -> 一条 D8 的 TX_PULSES JSON 命令。
    最多 9 通道(ch0..ch8); 超出部分丢弃(D8 只有 9 槽)。
    """
    chans = channels[:SLOT_WORDS]
    slots = [slot_word(f, r, g, b) for (f, r, g, b) in chans]
    slots += [0] * (SLOT_WORDS - len(slots))   # 不足 9 槽补灭
    durations = build_d8_transaction(slots, burst_frames=burst_frames)
    return tx_pulses_command(durations, request_id=request_id,
                             frequency_hz=frequency_hz, power_dbm=power_dbm)


# ============================================================================
# 紧凑命令 (TX_D8 / TX_BYTES)
# 旧路径 (TX_PULSES) 要把每帧 ~340 个 durations_us 整数写成 JSON:
#   D8 6 帧爆发 = ~10KB -> 921600 波特下光串口就要 ~110ms,
#   再加上固件解析 2000 个整数, 完全吃掉了压缩空口时间的收益。
# 新路径把"组帧"留在上位机(已验证), 固件只做 0,1,d 展开 + 游程编码 + RMT:
#   TX_D8   : 发 9 个 16bit 槽字, 固件自己加 D8 头/校验和/PHASE   (~170B)
#   TX_BYTES: 发帧字节的 hex, 适用于 00/C0/A6/DA 等短帧          (~150B)
# ============================================================================
D8_FRAME_BYTES = 21
D8_SYMBOLS = len(AIR_PREFIX) + D8_FRAME_BYTES * 8 * 3      # 521 (17 + 168*3)
AIR_GAP_US = 850          # 参考实现在 250us 下的帧间低电平
# 实测 (ESP32-S3 + CC1101 -> 荧光棒): bit_us=250/200 可解, 190 及以下不再解码,
# 所以单帧 D8 的空口下限就是 521 x 200us = 104.2ms, 7 字节帧在 250us 下 46ms。
MIN_BIT_US = 200
MAX_FRAME_BYTES = 64


def frame_duration_us(frame: bytes, bit_us: int = BIT_US) -> int:
    """一帧的空口时长 (us)。"""
    return (len(AIR_PREFIX) + len(frame) * 8 * 3) * bit_us


def tx_bytes_command(frame: bytes, request_id: str = "1",
                     frequency_hz: int = DEFAULT_FREQ_HZ,
                     power_dbm: int = DEFAULT_POWER_DBM, repeat: int = 1,
                     bit_us: int = BIT_US, gap_us: int = 20000) -> dict:
    """任意帧字节 -> 紧凑 TX_BYTES 命令 (固件做展开 + 游程编码)。"""
    if not 1 <= len(frame) <= MAX_FRAME_BYTES:
        raise ValueError(f"frame must be 1..{MAX_FRAME_BYTES} bytes")
    if not 1 <= int(repeat) <= 10:
        raise ValueError("repeat must be 1..10")
    return {
        "id": request_id,
        "cmd": "TX_BYTES",
        "args": {
            "data_hex": frame.hex().upper(),
            "repeat": int(repeat),
            "bit_us": int(bit_us),
            "gap_us": int(gap_us),
            "frequency_hz": int(frequency_hz),
            "power_dbm": int(power_dbm),
        },
    }


def d8_slots(channels: Sequence[Tuple[int, int, int, int]]) -> List[int]:
    """CSV 通道 (func,r,g,b) -> 定长 9 个 D8 槽字 (不足补灭)。"""
    chans = list(channels)[:SLOT_WORDS]
    slots = [slot_word(f, r, g, b) for (f, r, g, b) in chans]
    return slots + [0x0000] * (SLOT_WORDS - len(slots))


def tx_d8_command(channels: Sequence[Tuple[int, int, int, int]], request_id: str = "1",
                  frequency_hz: int = DEFAULT_FREQ_HZ,
                  power_dbm: int = DEFAULT_POWER_DBM, burst_frames: int = 6,
                  bit_us: int = BIT_US, gap_us: int = AIR_GAP_US) -> dict:
    """CSV 通道 -> 紧凑 TX_D8 命令 (固件内部组 9 槽 D8 帧 + 校验和)。"""
    return {
        "id": request_id,
        "cmd": "TX_D8",
        "args": {
            "slots": d8_slots(channels),
            "burst": int(burst_frames),
            "bit_us": int(bit_us),
            "gap_us": int(gap_us),
            "frequency_hz": int(frequency_hz),
            "power_dbm": int(power_dbm),
        },
    }


def d8_air_time_us(burst_frames: int = 6, bit_us: int = BIT_US,
                   gap_us: int = AIR_GAP_US) -> int:
    """一条 TX_D8 命令占用的空口时长 (us)。"""
    n = max(1, min(int(burst_frames), len(D8_PHASES)))
    return n * D8_SYMBOLS * bit_us + (n - 1) * gap_us


def tx_air_time_us(frame: bytes, repeat: int = 1, bit_us: int = BIT_US,
                   gap_us: int = 20000) -> int:
    """一条 TX_BYTES 命令占用的空口时长 (us, 含重复间隔)。"""
    n = max(1, int(repeat))
    return n * frame_duration_us(frame, bit_us) + (n - 1) * gap_us


def mode_interval(mode: str, burst_frames: int = 6, bit_us: int = BIT_US,
                  gap_us: int = AIR_GAP_US) -> int:
    """某模式下一条命令的空口时长(ms), 用于限速。"""
    if mode == "d8":
        return (d8_air_time_us(burst_frames, bit_us, gap_us) + 999) // 1000
    return MODE_INTERVAL_MS.get(mode, 380)


def is_blackout(frame_type: str, channels: Sequence[Tuple[int, int, int, int]]) -> bool:
    """是否黑场帧(全通道 RGB 全 0, 或 frame_type=blackout)。"""
    if (frame_type or "").strip().lower() == "blackout":
        return True
    return all(r == 0 and g == 0 and b == 0 for (_f, r, g, b) in channels) if channels else True
