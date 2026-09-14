# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""lightstick-player 核心逻辑单测: CSV 解析 / D8 命令构造 / 时间轴调度。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import csv_loader
import d8
import protocol
import timeline as timeline_mod

PASS = FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL: " + msg)


# ---- d8: slot word ----
check(d8.slot_word(0, 15, 8, 0) == 0x0F80, "slot_word(0,15,8,0)==0x0F80")
check(d8.slot_word(3, 15, 15, 15) == 0x3FFF, "slot_word(3,15,15,15)==0x3FFF")
check(d8.slot_word(0, 0, 0, 0) == 0x0000, "slot_word(0,0,0,0)==0")

# ---- d8: command ----
cmd = d8.build_d8_command([(0, 15, 8, 0)])
check(cmd.startswith("d8 "), "d8 command prefix")
words = cmd.split()
check(len(words) == 1 + d8.SLOT_WORDS, f"d8 has 9 slot words, got {len(words)-1}")
check(words[1] == "0F80", "first slot word 0F80")
check(words[9] == "0000", "padding channel = 0000")
# 10 通道: 只取前 9
cmd10 = d8.build_d8_command([(1,2,3,4)]*10)
check(len(cmd10.split()) == 1 + d8.SLOT_WORDS, "10-channel input still 9 words")

# ---- csv_loader ----
CSV = """frame_time_ms,frame_id,frame_type,marker,ch0_function,ch0_red,ch0_green,ch0_blue,ch1_function,ch1_red,ch1_green,ch1_blue,ch2_function,ch2_red,ch2_green,ch2_blue,ch3_function,ch3_red,ch3_green,ch3_blue,ch4_function,ch4_red,ch4_green,ch4_blue,ch5_function,ch5_red,ch5_green,ch5_blue,ch6_function,ch6_red,ch6_green,ch6_blue,ch7_function,ch7_red,ch7_green,ch7_blue,ch8_function,ch8_red,ch8_green,ch8_blue
1000,1,color,,0,15,8,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
0,0,blackout,,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
2000,2,color,drop,0,0,0,15,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
"""
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sample.csv")
with open(path, "w", encoding="utf-8") as f:
    f.write(CSV)

frames = csv_loader.load_csv(path)
check(len(frames) == 3, f"3 frames, got {len(frames)}")
# 按时间排序
check([f.frame_id for f in frames] == [0, 1, 2], "frames sorted by time (blackout first)")
check(frames[0].time_ms == 0 and frames[0].frame_type == "blackout", "blackout frame at t=0")
check(frames[1].time_ms == 1000, "frame 1 at 1000ms")
check(frames[1].channels[0] == (0, 15, 8, 0), "ch0 = func0 r15 g8 b0")
check(frames[2].marker == "drop", "marker parsed")
check(frames[2].channels[0] == (0, 0, 0, 15), "frame2 ch0 blue=15")

# 通道数检测
header = "frame_time_ms,ch0_function,ch0_red,ch0_green,ch0_blue,ch1_function,ch1_red,ch1_green,ch1_blue".split(",")
check(csv_loader.detect_channel_count(header) == 2, "detect 2 channels")

# ---- protocol: 00/C0/A6/DA/D8 原始帧 (金标验证, 与 Lightstick-Lab 文档一致) ----
def _hex(b):
    return " ".join(f"{x:02X}" for x in b)

check(_hex(protocol.build_zone_frame(0xFF, 0xFF, 0xFF, 0x00, 0x00)) == "00 FF FF FF 00 00 93",
      "00 全区暗 = 00 FF FF FF 00 00 93")
check(_hex(protocol.build_zone_frame(0xFF, 0xFF, 0xFF, 0x01, 0x00)) == "00 FF FF FF 01 00 94",
      "00 全区常亮红 = 00 FF FF FF 01 00 94")
check(_hex(protocol.build_zone_frame(0xFF, 0xFF, 0xFF, 0x01, 0xAA)) == "00 FF FF FF 01 AA 3E",
      "00 保色改常亮 = 00 FF FF FF 01 AA 3E")
check(_hex(protocol.build_c0_frame([0,1,2,3,4,5,6,7,8,9])) == "C0 01 23 45 67 89 AF",
      "C0 = C0 01 23 45 67 89 AF")
check(_hex(protocol.build_a6_frame(7)) == "A6 07 43", "A6 = A6 07 43")
check(_hex(protocol.build_da_frame(0xFF, 0xFF)) == "DA FF FF FF 01 00 6E",
      "DA = DA FF FF FF 01 00 6E")
check(_hex(protocol.build_d8_frame([0x1234]*9, 2)) ==
      "D8 48 D0 48 D0 48 D0 48 D0 48 D0 48 D0 48 D0 48 D0 48 D0 02 48",
      "D8 ROL16 + 大端 + checksum")

# 命令串
check(protocol.zone_command(0xFF, 0xFF, 0x01, 0x00) == "zone FF FF 01 00", "zone 命令串")
check(protocol.c0_command([0,1,2,3,4,5,6,7,8,9]) == "c0 0 1 2 3 4 5 6 7 8 9", "c0 命令串")
check(protocol.a6_command(7) == "a6 7", "a6 命令串")
check(protocol.da_command() == "da FF FF", "da 命令串")

# ---- 空口编码 / TX_PULSES (与 Lightstick-Lab _common.py 逐字节一致) ----
# 前导 + 01+d
bits = protocol.frame_to_bits(b"\x00\x96")
check(bits == "11111111000011110" + "010010010010010010010010" + "011010010011010011011010",
      "frame_to_bits 前导+01+d")
# 游程编码: 前导 11111111000011110 -> [2000,1000,1000,500]
check(protocol.bits_to_durations("11111111000011110") == [2000, 1000, 1000, 250],
      "bits_to_durations 前导游程")
# D8 事务: 9 槽红 -> 6 帧爆发, 总时长 785750us, 2040 个 duration
slots = [protocol.slot_word(0, 15, 0, 0)] + [0] * 8
dur = protocol.build_d8_transaction(slots)
check(len(dur) == 2040, f"D8 事务 2040 durations, got {len(dur)}")
check(sum(dur) == 785750, f"D8 事务总时长 785750us, got {sum(dur)}")
check(dur[0] == 2000 and dur[1] == 1000, "D8 事务起始=前导")
# TX_PULSES 命令结构
cmd = protocol.d8_tx_command([(0, 15, 0, 0)], request_id="7", frequency_hz=433920000, power_dbm=-20)
check(cmd["cmd"] == "TX_PULSES" and cmd["id"] == "7", "TX_PULSES cmd/id")
check(cmd["args"]["start_level"] == 1, "start_level=1")
check(cmd["args"]["frequency_hz"] == 433920000, "frequency_hz")
check(len(cmd["args"]["durations_us"]) == 2040, "durations_us 长度")
# 10 通道 -> 只取前 9
cmd10 = protocol.d8_tx_command([(1, 2, 3, 4)] * 10)
check(len(cmd10["args"]["durations_us"]) == 2040, "10通道仍9槽")

# ---- 调色板量化 + C0/00 短命令 ----
check(protocol.nearest_palette(15, 0, 0) == 0x00, "红 -> 色码0")
check(protocol.nearest_palette(0, 15, 0) == 0x01, "绿 -> 色码1")
check(protocol.nearest_palette(0, 0, 15) == 0x02, "蓝 -> 色码2")
check(protocol.nearest_palette(15, 15, 15) == 0x04, "白 -> 色码4")
check(protocol.nearest_palette(6, 12, 15) == 0x06, "浅蓝 -> 色码6")
check(protocol.FUNCTION_TO_STATE == {0: 1, 1: 2, 2: 3, 3: 4}, "function->state 映射")
# C0 命令: 10 色码, 单帧重复6次
cc0 = protocol.c0_tx_command([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
check(cc0["cmd"] == "TX_PULSES" and cc0["args"]["repeat"] == 6, "C0 repeat=6")
check(cc0["args"]["start_level"] == 1, "C0 start_level=1")
# 单帧(7字节)的 duration 数 = 前导17 + 7*8*3 = 185
check(len(cc0["args"]["durations_us"]) < 200, "C0 单帧 durations < 200")
# 00 命令
zz = protocol.zone_tx_command(0xFF, 0xFF, 0x01, 0x00)
check(zz["cmd"] == "TX_PULSES" and zz["args"]["repeat"] == 6, "zone repeat=6")
check(len(zz["args"]["durations_us"]) < 200, "zone 单帧 durations < 200")
# 模式限速间隔
check(protocol.MODE_INTERVAL_MS["d8"] == 786, "d8 限速 786ms")
check(protocol.MODE_INTERVAL_MS["c0"] == 380 and protocol.MODE_INTERVAL_MS["zone"] == 380, "c0/zone 限速 380ms")

# ---- burst / 黑场 / 模式分派 (修复 <786ms 播不动 + c0/zone 颜色异常) ----
import main as main_mod
_slots = [protocol.slot_word(0, 15, 0, 0)] + [0] * 8
d6 = protocol.build_d8_transaction(_slots, burst_frames=6)
d3 = protocol.build_d8_transaction(_slots, burst_frames=3)
d1 = protocol.build_d8_transaction(_slots, burst_frames=1)
check(len(d6) == 2040 and sum(d6) == 785750, "burst=6: 2040 durations / 785750us")
check(len(d3) == 1020 and sum(d3) == 392450, "burst=3: 1020 durations / 392450us (3x130250 + 2x850)")
check(len(d1) == 340 and sum(d1) == 130250, "burst=1: 340 durations / 130250us")
check(d3[0] == 2000 and d1[0] == 2000, "burst 起始都是前导")
check(protocol.mode_interval("d8", 6) == 786, "mode_interval d8 burst6 = 786")
check(protocol.mode_interval("d8", 3) == 393, "mode_interval d8 burst3 = 393")
check(protocol.is_blackout("blackout", [(0, 0, 0, 0)]), "is_blackout 按 frame_type")
check(protocol.is_blackout("color", [(0, 0, 0, 0)]), "is_blackout 按全 0")
check(not protocol.is_blackout("color", [(0, 15, 0, 0)]), "非黑场")

# c0 黑场 -> 用 00 关灯(而不是被量化成深绿 0x0D)
cmds0, _c = main_mod.commands_for_frame(frames[0], "c0", 433920000, -20, 6, False)
check(len(cmds0) == 1 and cmds0[0]["cmd"] == "TX_BYTES"
      and cmds0[0]["args"]["repeat"] == 1,
      "c0 黑场 -> 1 条紧凑 TX_BYTES 命令 (repeat=1 默认)")
check(protocol.nearest_palette(0, 0, 0) == 0x0D, "RGB(0,0,0) 最近的是 0x0D(深绿) -- 所以黑场必须特判")
# c0 彩色帧 -> 1 条 C0
cmds1, _c = main_mod.commands_for_frame(frames[1], "c0", 433920000, -20, 6, False)
check(len(cmds1) == 1, "c0 彩色 -> 1 条命令")
# zone 多色 -> 按 (状态,色码) 分组, 多条命令

class _FR:
    pass


_fr = _FR()
_fr.frame_type = "color"
_fr.frame_id = 0
_fr.channels = [(0, 15, 0, 0), (0, 0, 15, 0), (0, 0, 0, 15)]
cmdsz, costz = main_mod.commands_for_frame(_fr, "zone", 433920000, -20, 6, False)
check(len(cmdsz) == 3, f"zone 3 种颜色 -> 3 条命令, got {len(cmdsz)}")
_per = main_mod.frames_cost_ms("zone", 6, protocol.BIT_US, protocol.AIR_GAP_US, 1, True)
check(costz == _per * 3, f"zone 成本 = 每帧空口 {_per}ms x 命令数")
_fr.channels = [(0, 15, 0, 0)] * 3
cmdsz2, _c = main_mod.commands_for_frame(_fr, "zone", 433920000, -20, 6, False)
check(len(cmdsz2) == 1, "zone 同色 -> 合并成 1 条命令")

# ---- timeline ----
sent = []
clock = {"t": 0}
tl = timeline_mod.Timeline(frames, lambda f: sent.append(f.frame_id),
                           clock_ms=lambda: clock["t"])
n, done = tl.tick()
check(n == 1 and sent == [0], "at t=0 only blackout (id0) sent")
check(not done, "not done")
clock["t"] = 500
tl.tick()
check(sent == [0], "at 500ms nothing new")
clock["t"] = 1000
tl.tick()
check(sent == [0, 1], "at 1000ms frame1 sent")
clock["t"] = 1500
tl.tick()
check(sent == [0, 1], "at 1500ms nothing new")
clock["t"] = 2000
n, done = tl.tick()
check(sent == [0, 1, 2] and done, "at 2000ms all sent and done")

# ---- timeline 限速 (--throttle): 帧等待不丢弃 ----
class _F:
    def __init__(self, t, i):
        self.time_ms = t
        self.frame_id = i

# 帧时间 0/500/1000ms, 限速 786ms -> 发送节奏被拉慢, 但一帧都不丢
frames3 = [_F(0, 0), _F(500, 1), _F(1000, 2)]
sent2 = []
clock2 = {"t": 0}
tl2 = timeline_mod.Timeline(frames3, lambda f: sent2.append(f.frame_id),
                            clock_ms=lambda: clock2["t"], min_interval_ms=786)
clock2["t"] = 0
tl2.tick()
check(sent2 == [0], "限速: t=0 发第1帧")
clock2["t"] = 500
tl2.tick()
check(sent2 == [0], "限速: t=500 距上帧不足786ms, 不发第2帧")
clock2["t"] = 786
tl2.tick()
check(sent2 == [0, 1], "限速: t=786 才发第2帧(本应500ms, 被延迟)")
clock2["t"] = 1500
tl2.tick()
check(sent2 == [0, 1], "限速: t=1500 距上帧不足786ms, 不发第3帧")
clock2["t"] = 1572
n, done = tl2.tick()
check(sent2 == [0, 1, 2] and done, "限速: t=1572 发第3帧, 全部发完且无丢失")

# ---- 紧凑命令层 (TX_D8 / TX_BYTES) ----
f7 = protocol.build_c0_frame([0] * 10)
f21 = protocol.build_d8_frame([0x0F00] * 9, 2)

# 帧空口时长 = 符号数 x 符号宽度, 必须与逐位展开+游程编码的求和一致
check(protocol.frame_duration_us(f7) == sum(protocol.bits_to_durations(protocol.frame_to_bits(f7))),
      "frame_duration_us 与 bits_to_durations 求和一致 (7 字节)")
check(protocol.frame_duration_us(f21) == sum(protocol.bits_to_durations(protocol.frame_to_bits(f21))),
      "frame_duration_us 与 bits_to_durations 求和一致 (D8 21 字节)")
check(protocol.frame_duration_us(f7) == 185 * 250, "7 字节帧 @250us = 46250us")
check(protocol.frame_duration_us(f21) == 521 * 250, "D8 帧 @250us = 130250us")
check(protocol.frame_duration_us(f21, 200) == 521 * 200, "D8 帧 @200us = 104200us")

# TX_BYTES: 帧字节 -> hex, 固件展开
cmd_b = protocol.tx_bytes_command(f7, request_id="9", repeat=1, bit_us=250)
check(cmd_b["cmd"] == "TX_BYTES", "TX_BYTES 命令名")
check(cmd_b["args"]["data_hex"] == f7.hex().upper(), "TX_BYTES data_hex 与帧字节一致")
check(len(cmd_b["args"]["data_hex"]) == 14, "7 字节帧 -> 14 个 hex 字符")
check(protocol.tx_air_time_us(f7, 1, 250) == 46250, "TX_BYTES 单帧空口 46250us")
check(protocol.tx_air_time_us(f7, 3, 250, 20000) == 3 * 46250 + 2 * 20000,
      "TX_BYTES 重复 3 次的空口时长含重复间隔")

# 紧凑命令载荷必须远小于旧的 durations_us 载荷 (这是能把帧间隔压下去的原因)
_legacy = protocol.tx_pulses_command(protocol.build_d8_transaction([0x0F00] * 9, burst_frames=6))
_compact = protocol.tx_d8_command([(0, 15, 0, 0)] * 9, burst_frames=6)
_len_legacy = len(json.dumps(_legacy, separators=(",", ":")))
_len_compact = len(json.dumps(_compact, separators=(",", ":")))
check(_len_legacy > 8000, f"legacy D8 TX_PULSES 载荷 >8KB, got {_len_legacy}")
check(_len_compact < 250, f"紧凑 TX_D8 载荷 <250B, got {_len_compact}")
check(_len_legacy > _len_compact * 40, "紧凑载荷比 legacy 小 40 倍以上")

# TX_D8: 9 个槽字, 超出截断, 不足补灭
check(protocol.d8_slots([(1, 2, 3, 4)] * 12) == [protocol.slot_word(1, 2, 3, 4)] * 9,
      "d8_slots 截断到 9 槽")
check(protocol.d8_slots([(0, 15, 0, 0)])[:1] == [0x0F00]
      and protocol.d8_slots([(0, 15, 0, 0)])[1:] == [0] * 8,
      "d8_slots 不足 9 槽补 0x0000")
_cmd_d8 = protocol.tx_d8_command([(0, 15, 0, 0)] * 9, burst_frames=1, bit_us=200)
check(_cmd_d8["cmd"] == "TX_D8" and len(_cmd_d8["args"]["slots"]) == 9, "TX_D8 命令 9 个槽字")
check(_cmd_d8["args"]["bit_us"] == 200 and _cmd_d8["args"]["burst"] == 1, "TX_D8 透传 bit_us/burst")

# 空口时长表 (硬件实测: 荧光棒在 200us 可解, 190us 以下不再解码)
check(protocol.d8_air_time_us(6) == 785750, "D8 6 帧 @250us = 785750us")
check(protocol.d8_air_time_us(1, 250) == 130250, "D8 单帧 @250us = 130250us")
check(protocol.d8_air_time_us(1, 200) == 104200, "D8 单帧 @200us = 104200us")
check(protocol.mode_interval("d8", 6) == 786, "mode_interval d8 burst6 仍是 786ms")
check(protocol.mode_interval("d8", 1, 200) == 105, "mode_interval d8 单帧 200us = 105ms")
check(protocol.MIN_BIT_US == 200, "MIN_BIT_US = 200 (硬件实测下限)")

# 紧凑模式下的整帧成本 (main.frames_cost_ms)
import main as main_mod  # noqa: E402
check(main_mod.frames_cost_ms("d8", 1, 200, 850, 1, True) == 105, "d8 单帧 200us 成本 105ms")
check(main_mod.frames_cost_ms("c0", 6, 250, 850, 1, True) == 47, "c0 单帧成本 47ms")
check(main_mod.frames_cost_ms("c0", 6, 250, 850, 6, False) == 378, "c0 legacy 重复 6 次成本 378ms")

# ---- timeline drop_late: 跟不上就丢帧, 保住与视频的同步 ----
# 帧在 0/300/600/900ms, 但每条命令要占 500ms -> 只能显示一半的帧。
frames4 = [_F(0, 0), _F(300, 1), _F(600, 2), _F(900, 3)]
sent4 = []
clock4 = {"t": 0}
tl4 = timeline_mod.Timeline(frames4, lambda f: sent4.append(f.frame_id),
                            clock_ms=lambda: clock4["t"], min_interval_ms=500,
                            drop_late=True)
tl4.tick()
check(sent4 == [0], "drop_late: t=0 发第1帧, 之后 500ms 内不能再发")
clock4["t"] = 300
tl4.tick()
check(sent4 == [0] and tl4.dropped == 1,
      f"drop_late: t=300 第2帧过期且被限速, 直接跳过, got sent={sent4} dropped={tl4.dropped}")
clock4["t"] = 600
tl4.tick()
check(sent4 == [0, 2], f"drop_late: t=600 限额已过, 发当前该显示的帧2, got {sent4}")
clock4["t"] = 900
n, done = tl4.tick()
check(sent4 == [0, 2] and tl4.dropped == 2 and done,
      f"drop_late: t=900 第4帧同样被跳过, 队列走完, got sent={sent4}")

# 不开 drop_late 时行为不变: 排队等待, 一帧不丢(但会越跑越落后)
sent5 = []
clock5 = {"t": 0}
tl5 = timeline_mod.Timeline(frames4, lambda f: sent5.append(f.frame_id),
                            clock_ms=lambda: clock5["t"], min_interval_ms=500,
                            drop_late=False)
tl5.tick()
clock5["t"] = 300
tl5.tick()
check(sent5 == [0] and tl5.dropped == 0, "无 drop_late: t=300 被限速挡住, 不丢帧也不发帧")
clock5["t"] = 600
tl5.tick()
check(sent5 == [0, 1] and tl5.dropped == 0,
      f"无 drop_late: t=600 才补发第2帧(已迟到 300ms), got {sent5}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
