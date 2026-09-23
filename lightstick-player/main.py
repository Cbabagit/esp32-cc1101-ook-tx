# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""lightstick-player: 播放 CSV 灯光序列到 ESP32-S3 + CC1101 桥接固件, 并同步本地视频。

目标固件: Lightstick-Lab (ESP32-S3 + CC1101), 串口 JSON 命令 API (921600)。

两条下发路径 (--legacy 切换):
  compact (默认):
    D8  -> TX_D8   (9 个 16bit 槽字, ~167B, 固件内部组帧+校验和)
    C0/00 -> TX_BYTES (7 字节帧的 hex, ~146B)
    单帧命令载荷 150B 量级, 串口本身几乎不耗时。
  legacy:
    TX_PULSES + durations_us, D8 6 帧爆发要 ~10KB JSON (921600 波特下 ~110ms)。

实测空口时长 (ESP32-S3 + CC1101 -> 荧光棒):
  D8  单帧: 250us 符号 130ms / 200us 符号 104ms  <- 200us 是荧光棒解码下限
  C0/00 单帧 7 字节: 250us 符号 46ms (重复 N 次则 xN)
因此 D8 模式的帧间隔物理下限约 104ms; 需要 <100ms 的帧间隔请用 c0/zone 模式。

用法示例:
  python main.py show.csv --dry-run                    # 预览命令(不接硬件)
  python main.py show.csv --port COM10                 # 默认: d8 + 紧凑命令
  python main.py show.csv --port COM10 --mode c0       # 46ms/帧, 可跑 <100ms 帧间隔
  python main.py show.csv --port COM10 --burst 1 --bit-us 200   # D8 压到最快
  python main.py show.csv --port COM10 --video a.mp4   # CSV + 视频同步
  python main.py --list-ports
"""
from __future__ import annotations

import argparse
import sys
import time

import csv_loader
import protocol
import timeline as timeline_mod
import transport
import video as video_mod


def frames_cost_ms(mode: str, burst: int, bit_us: int, gap_us: int, repeat: int,
                   compact: bool) -> int:
    """某模式一条命令的空口时长 (ms), 用于限速与跟不上时的提示。"""
    if mode == "d8":
        if not compact:
            return protocol.mode_interval("d8", burst)
        return protocol.mode_interval("d8", burst, bit_us, gap_us)
    # 00 / C0 都是 7 字节帧, 时长只取决于帧长
    probe = b"\x00" * 7
    if not compact:
        return (protocol.tx_air_time_us(probe, repeat, protocol.BIT_US, 20000) + 999) // 1000
    return (protocol.tx_air_time_us(probe, repeat, bit_us, gap_us) + 999) // 1000


def commands_for_frame(frame, mode: str, freq: int, power: int, burst: int, warn: bool,
                       bit_us: int = protocol.BIT_US, gap_us: int = protocol.AIR_GAP_US,
                       repeat: int = 1, compact: bool = True):
    """CSV 帧 -> (命令列表, 本帧空口成本 ms)。"""
    chans = frame.channels
    cost_ms = frames_cost_ms(mode, burst, bit_us, gap_us, repeat, compact)

    if mode == "d8":
        if warn and len(chans) > protocol.SLOT_WORDS:
            print(f"[warn] 第 {frame.frame_id} 帧含 {len(chans)} 通道, D8 只取前 {protocol.SLOT_WORDS} 个")
        if compact:
            cmd = protocol.tx_d8_command(chans, frequency_hz=freq, power_dbm=power,
                                         burst_frames=burst, bit_us=bit_us, gap_us=gap_us)
        else:
            cmd = protocol.d8_tx_command(chans, frequency_hz=freq, power_dbm=power,
                                         burst_frames=burst)
        return [cmd], cost_ms

    if mode == "c0":
        # C0 没有"关"色码 -> 黑场帧改用 00 命令 state=0 关灯(否则会被量化成深绿)
        if protocol.is_blackout(frame.frame_type, chans):
            cmd = protocol.zone_tx_command(0xFF, 0xFF, 0x00, 0x00, frequency_hz=freq,
                                           power_dbm=power, repeat=repeat, compact=compact,
                                           bit_us=bit_us, gap_us=gap_us)
            return [cmd], cost_ms
        colors = [protocol.nearest_palette(r, g, b) for (_f, r, g, b) in chans[:10]]
        while len(colors) < 10:                      # 补齐: 重复最后一个颜色(而不是 0=红)
            colors.append(colors[-1] if colors else 0)
        cmd = protocol.c0_tx_command(colors, frequency_hz=freq, power_dbm=power,
                                     repeat=repeat, compact=compact,
                                     bit_us=bit_us, gap_us=gap_us)
        return [cmd], cost_ms

    # zone (00): 按 (状态, 色码) 分组, 每组一条命令, 掩码覆盖该组分区
    groups = {}   # (state, color) -> [m1, m2]
    for i, (f_, r, g, b) in enumerate(chans[:16]):
        off = protocol.is_blackout(frame.frame_type, [(f_, r, g, b)])
        st = 0x00 if off else protocol.FUNCTION_TO_STATE.get(f_, 0x01)
        col = protocol.nearest_palette(r, g, b)
        m1, m2 = groups.get((st, col), [0, 0])
        if i < 8:
            m1 |= (1 << i)
        else:
            m2 |= (1 << (i - 8))
        groups[(st, col)] = [m1, m2]
    if not groups:
        groups[(0x00, 0x00)] = [0xFF, 0xFF]
    cmds = [protocol.zone_tx_command(m1, m2, st, col, frequency_hz=freq, power_dbm=power,
                                     repeat=repeat, compact=compact,
                                     bit_us=bit_us, gap_us=gap_us)
            for (st, col), (m1, m2) in groups.items()]
    return cmds, cost_ms * len(cmds)


def build_sender(tx, mode: str, warn_on_extra: bool, freq: int, power: int, burst: int,
                 bit_us: int = protocol.BIT_US, gap_us: int = protocol.AIR_GAP_US,
                 repeat: int = 1, compact: bool = True):
    seq = [0]

    def send(frame):
        cmds, cost = commands_for_frame(frame, mode, freq, power, burst, warn_on_extra,
                                        bit_us, gap_us, repeat, compact)
        for cmd in cmds:
            seq[0] += 1
            cmd["id"] = str(seq[0])
            tx.send_json(cmd)
        return cost   # 告诉时间轴本帧空口成本(ms), 用于限速
    return send


def _reply_logger(prefix="设备"):
    def on_reply(reply):
        status = reply.get("status") or ("ok" if reply.get("ok") else "?")
        if reply.get("error"):
            print(f"[{prefix}] 错误: {reply.get('error')}")
        elif reply.get("event"):
            print(f"[{prefix}] {reply.get('event')}: {status}")
        elif reply.get("result"):
            print(f"[{prefix}] {reply.get('id', '')} {status}")
    return on_reply


def list_devices(args):
    """打印能找到的板子: 串口 / UDP 广播 / BLE 扫描。"""
    print("串口:")
    ports = transport.list_ports()
    for port in ports:
        print(f"  usb:{port}")
    if not ports:
        print("  (无)")

    print(f"Wi-Fi UDP (广播 {transport.UDP_DISCOVERY_PORT}):")
    info = transport.discover(timeout=2.0)
    if info:
        print(f"  udp:{info.get('ip')}  {info.get('device')}  fw={info.get('firmware_version')}"
              f"  host={info.get('hostname')}")
    else:
        print("  (没回应; 板子可能没连上同一个 Wi-Fi)")

    print("BLE:")
    try:
        import asyncio
        from bleak import BleakScanner
        found = asyncio.run(BleakScanner.discover(timeout=6.0))
        hits = [d for d in found
                if (getattr(d, "name", None) or "").lower().startswith("lightstick")]
        for device in hits:
            print(f"  ble:{device.name}  ({device.address})")
        if not hits:
            print(f"  (没扫到 Lightstick*, 共扫到 {len(found)} 个设备)")
    except Exception as exc:
        print(f"  (BLE 扫描失败: {exc})")
    return 0


def connect_transport(args):
    """按 --transport 建立传输, 返回 (tx, error)。"""
    if args.dry_run or args.transport == "dry":
        return transport.DryRunTransport(), None

    on_reply = _reply_logger()
    attempts = []
    if args.transport == "auto":
        if args.port:
            attempts.append("usb:" + args.port)
        elif args.host:
            attempts.append("udp:" + args.host)
        else:
            # 没给任何地址: 先试广播发现, 再试 BLE (BLE 不需要知道地址)
            attempts += ["udp", "ble"]
    elif args.transport == "usb":
        if not args.port:
            return None, "usb 传输需要 --port (如 --port COM10)"
        attempts.append("usb:" + args.port)
    elif args.transport == "udp":
        attempts.append("udp:" + args.host if args.host else "udp")
    elif args.transport == "ble":
        attempts.append("ble:" + args.ble_name if args.ble_name else "ble")
    else:
        return None, f"未知的 --transport: {args.transport}"

    problems = []
    for spec in attempts:
        try:
            return transport.open_transport(spec, log=print, on_reply=on_reply,
                                            baud=args.baud), None
        except Exception as exc:
            problems.append(f"  {spec}: {exc}")
    hint = ""
    if args.transport == "auto":
        hint = (chr(10) + "提示: 用 --transport 指定 usb / udp / ble, "
                "或用 --list-devices 看看板子在哪。")
    return None, "连不上板子:" + chr(10) + chr(10).join(problems) + hint


def run(args):
    frames = csv_loader.load_csv(args.csv)
    nch = len(frames[0].channels) if frames else 0
    print(f"加载 {len(frames)} 帧, 通道数={nch}")
    if args.mode == "d8":
        print(f"模式 d8, 爆发 {args.burst} 帧, 符号 {args.bit_us}us, 命令 {'紧凑 TX_D8' if not args.legacy else 'legacy TX_PULSES'}")
    else:
        print(f"模式 {args.mode}, 每帧重复 {args.repeat} 次, 符号 {args.bit_us}us, 命令 {'紧凑 TX_BYTES' if not args.legacy else 'legacy TX_PULSES'}")

    if args.probe:
        if not args.video:
            print("--probe 需要 --video")
            return 1
        info = video_mod.probe_video(args.video)
        print(f"视频: {info}")
        return 0

    tx, error = connect_transport(args)
    if tx is None:
        print(error)
        return 1
    print(f"已连接: {tx.describe()}")

    sender = build_sender(tx, args.mode, warn_on_extra=not args.quiet,
                          freq=args.frequency, power=args.power, burst=args.burst,
                          bit_us=args.bit_us, gap_us=args.gap_us, repeat=args.repeat,
                          compact=not args.legacy)

    # 限速: 默认在 CSV 帧间隔小于协议一条命令时长时自动开启
    proto_interval = frames_cost_ms(args.mode, args.burst, args.bit_us, args.gap_us,
                                    args.repeat, not args.legacy)
    gaps = [frames[i + 1].time_ms - frames[i].time_ms for i in range(len(frames) - 1)]
    min_gap = min(gaps) if gaps else 1e9
    if args.no_throttle:
        min_interval = 0
        print("限速已关闭(--no-throttle): 帧可能被丢弃/合并")
    elif args.throttle or min_gap < proto_interval:
        min_interval = proto_interval
        if min_gap < proto_interval:
            print(f"提示: CSV 最小帧间隔 {min_gap:.0f}ms < 本模式最快 {proto_interval}ms, 已自动限速")
        else:
            print(f"已开启限速: 每帧间隔 >= {min_interval}ms")
        if args.mode == "d8":
            print("       D8 单帧空口 = 521 符号 x 符号宽度, 荧光棒在 200us 以下不再解码,")
            print("       所以 D8 的帧间隔下限约 130ms(250us) / 104ms(200us)。")
            print("       需要 <100ms 帧间隔请用 --mode c0 或 --mode zone (7 字节帧, 46ms/次)。")
        if args.drop_late:
            print("       已开启 --drop-late: 跟不上的帧会被跳过, 保持与视频同步。")
        else:
            print("       未开 --drop-late: 跟不上的帧会排队等待, 会逐渐落后于视频。")
    else:
        min_interval = 0
    tl = timeline_mod.Timeline(frames, sender, min_interval_ms=min_interval,
                               drop_late=args.drop_late)

    vp = None
    if args.video:
        vp = video_mod.VlcPlayer(args.video)
        vp.set_fullscreen(args.fullscreen)
        vp.play()
        for _ in range(200):
            if vp.is_playing():
                break
            time.sleep(0.01)
        tl = timeline_mod.Timeline(frames, sender, clock_ms=vp.time_ms,
                                   min_interval_ms=min_interval, drop_late=args.drop_late)

    print("开始播放 (Ctrl+C 停止)")
    try:
        while True:
            sent, done = tl.tick()
            if done and (vp is None or not vp.is_playing()):
                break
            time.sleep(0.005)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if vp:
            vp.stop()
        tx.close()

    if tl.dropped:
        print(f"共发送 {tl.progress} 帧 (跳过 {tl.dropped} 帧以跟上视频)")
    else:
        print(f"共发送 {tl.progress} 帧")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="lightstick-player CSV 播放上位机")
    ap.add_argument("csv", nargs="?", help="CSV 序列文件")
    ap.add_argument("--transport", choices=("auto", "usb", "udp", "ble", "dry"),
                    default="auto",
                    help="连接方式: usb 串口 / udp Wi-Fi / ble 蓝牙 / dry 空跑 (默认 auto)")
    ap.add_argument("--port", help="串口(如 COM10); 等价于 --transport usb")
    ap.add_argument("--host", help="Wi-Fi UDP 目标 IP; 留空则广播自动发现")
    ap.add_argument("--ble-name", help="BLE 设备名 (默认 Lightstick N16R8)")
    ap.add_argument("--list-devices", action="store_true",
                    help="列出串口 / UDP 发现的板子 / BLE 设备后退出")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--video", help="同步播放的视频文件")
    ap.add_argument("--probe", action="store_true", help="仅探测视频信息后退出")
    ap.add_argument("--dry-run", action="store_true", help="不接串口, 只打印命令")
    ap.add_argument("--frequency", type=int, default=protocol.DEFAULT_FREQ_HZ, help="射频频率 Hz (默认 433920000)")
    ap.add_argument("--power", type=int, default=protocol.DEFAULT_POWER_DBM, help="发射功率 dBm (默认 -20)")
    ap.add_argument("--fullscreen", action="store_true", help="视频全屏")
    ap.add_argument("--list-ports", action="store_true", help="列出串口后退出")
    ap.add_argument("--quiet", action="store_true", help="不打印超通道警告")
    ap.add_argument("--throttle", action="store_true", help="强制开启限速(帧等待不丢弃)")
    ap.add_argument("--no-throttle", action="store_true", help="关闭限速(帧可能被丢弃)")
    ap.add_argument("--drop-late", action="store_true",
                    help="跟不上的帧直接跳过(保持与视频同步), 而不是排队等待")
    ap.add_argument("--burst", type=int, choices=(1, 2, 3, 6), default=6,
                    help="D8 爆发帧数: 6=786ms(默认, 最可靠), 3=393ms, 2=262ms, 1=131ms")
    ap.add_argument("--bit-us", type=int, default=protocol.BIT_US,
                    help=f"每符号宽度 us (默认 {protocol.BIT_US}; 实测荧光棒下限 {protocol.MIN_BIT_US})")
    ap.add_argument("--gap-us", type=int, default=protocol.AIR_GAP_US,
                    help=f"D8 帧间低电平 us (默认 {protocol.AIR_GAP_US})")
    ap.add_argument("--repeat", type=int, default=1,
                    help="c0/zone 每帧重复次数 (默认 1; 2..6 更稳但更慢)")
    ap.add_argument("--legacy", action="store_true",
                    help="改用旧的 TX_PULSES + durations_us 下发(~10KB/条)")
    ap.add_argument("--mode", choices=("d8", "c0", "zone"), default="d8",
                    help="协议模式: d8=9槽RGB+function(默认), c0=A-J十区色码(快), zone=00分区(快)")
    args = ap.parse_args(argv)

    if args.list_ports:
        for p in transport.list_ports():
            print(p)
        return 0
    if args.list_devices:
        return list_devices(args)
    if not args.csv:
        ap.print_help()
        return 1
    if args.bit_us < protocol.MIN_BIT_US:
        print(f"[warn] --bit-us {args.bit_us} 低于实测下限 {protocol.MIN_BIT_US}us, 荧光棒很可能不再解码")
    if not 1 <= args.repeat <= 10:
        print("--repeat 必须在 1..10")
        return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
