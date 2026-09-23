# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
"""往板子发一条任意 JSON 命令并打印响应, 三种传输通用。

    py tools/send_cmd.py GET_NETWORK_STATUS
    py tools/send_cmd.py GET_INFO --transport ble
    py tools/send_cmd.py GET_STATUS --transport udp
    py tools/send_cmd.py GET_STATUS --transport usb --port COM10
    py tools/send_cmd.py '{"cmd":"REG_READ","args":{"reg":0}}'
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transport


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("payload", help="JSON 命令, 或直接给命令名")
    ap.add_argument("--transport", choices=("auto", "usb", "udp", "ble"), default="auto")
    ap.add_argument("--port", default=None, help="串口 (usb)")
    ap.add_argument("--host", default=None, help="UDP 目标 IP, 留空自动发现")
    ap.add_argument("--ble-name", default=None, help="BLE 设备名")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--wait", type=int, default=1, help="等几条响应 (TX 命令有 2 条)")
    args = ap.parse_args()

    payload = args.payload.strip()
    # 允许直接给命令名, 省得在 PowerShell 里跟引号打架
    if not payload.startswith("{"):
        payload = json.dumps({"cmd": payload})
    obj = json.loads(payload)
    obj.setdefault("id", "cli-%d" % int(time.time() * 1000))

    if args.transport == "usb":
        spec = "usb:" + (args.port or "COM10")
    elif args.transport == "udp":
        spec = "udp:" + args.host if args.host else "udp"
    elif args.transport == "ble":
        spec = "ble:" + args.ble_name if args.ble_name else "ble"
    elif args.port:
        spec = "usb:" + args.port
    elif args.host:
        spec = "udp:" + args.host
    else:
        spec = "ble:" + args.ble_name if args.ble_name else "ble"

    replies = []
    try:
        tx = transport.open_transport(spec, log=lambda s: print("  " + s),
                                      on_reply=replies.append)
    except Exception as exc:
        print("连接失败: %s" % exc)
        return 1
    print("已连接: %s" % tx.describe())

    tx.send_json(obj)
    deadline = time.time() + args.timeout
    while time.time() < deadline and len(replies) < args.wait:
        time.sleep(0.02)
    tx.close()

    if not replies:
        print("超时: 没收到 %s 的响应" % obj["id"])
        return 1
    for reply in replies:
        print(json.dumps(reply, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
