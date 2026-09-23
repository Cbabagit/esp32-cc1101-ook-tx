# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
"""BLE 真机测试: 连板子 -> 发命令 -> 收同步响应 + 异步 TX_COMPLETE。

需要板子上电、BLE 开着 (固件默认开)。板子不在就跳过, 不算失败。

    py tests/test_ble_live.py
    py tests/test_ble_live.py --name "Lightstick N16R8"
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transport

PASS = FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    spec = "ble:" + args.name if args.name else "ble"
    print("连接 %s ..." % spec)
    try:
        tx = transport.open_transport(spec, log=lambda s: print("  " + s),
                                      on_reply=None)
    except Exception as exc:
        print("跳过: BLE 连不上 (%s)" % exc)
        print("\n0 passed, 0 failed (已跳过)")
        return 0

    replies = []
    tx.on_reply = replies.append
    check(tx.kind == "ble", "传输类型是 ble")
    check(bool(tx.device_label), "连上了设备: %s" % tx.device_label)

    # ---- 1) 小命令: 同步响应 ----
    tx.send_json({"id": "live-1", "cmd": "GET_INFO"})
    deadline = time.time() + 6.0
    while time.time() < deadline and not replies:
        time.sleep(0.02)
    check(len(replies) >= 1, "GET_INFO 收到响应")
    if replies:
        first = replies[0]
        check(first.get("id") == "live-1", "响应 id 对得上")
        result = first.get("result") or {}
        check(result.get("cc1101_found") is True, "板子报告 CC1101 在位")
        check("ble" in result, "GET_INFO 里带 BLE 信息")

    # ---- 2) 大命令 (~170 字节): 验证 BLE 长写 + 异步通知 ----
    import protocol
    replies.clear()
    command = protocol.tx_d8_command([(0, 15, 0, 0)] * 9, request_id="live-2",
                                     burst_frames=1, bit_us=250)
    size = len(json.dumps(command, separators=(",", ":")))
    tx.send_json(command)
    deadline = time.time() + 8.0
    while time.time() < deadline and len(replies) < 2:
        time.sleep(0.02)
    check(len(replies) >= 1, "TX_D8 (%d 字节) 收到同步确认" % size)
    if replies:
        check(replies[0].get("result", {}).get("accepted") is True, "命令被接受")
    check(len(replies) >= 2, "收到异步 TX_COMPLETE 通知")
    if len(replies) >= 2:
        check(replies[1].get("event") == "TX_COMPLETE", "第二个响应是 TX_COMPLETE")
        # 本仓库的固件里协议是占位符, 所以这里预期是"构造不出波形"的错误。
        # 如果你已经补全了 buildD8Frame/appendFrameDurations, 这里会变成 success。
        status = (replies[1].get("result") or {}).get("status")
        error = replies[1].get("error", "")
        ok = status == "success" or "failed to build RMT pulse waveform" in error
        check(ok, "TX_COMPLETE 结果符合预期 (success 或占位符报错): %s%s"
              % (status, error))

    tx.close()
    print()
    print("%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
