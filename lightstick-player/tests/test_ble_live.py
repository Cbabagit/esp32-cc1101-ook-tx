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


def gui_check():
    """按 GUI 的方式连接一次 BLE (弹窗改成记录, 不阻塞)。"""
    import tkinter as tk
    import gui as gui_mod

    popups = []

    class FakeBox:
        def showerror(self, *args, **kwargs):
            popups.append(args)

        def showwarning(self, *args, **kwargs):
            popups.append(args)

        def showinfo(self, *args, **kwargs):
            popups.append(args)

    real_messagebox = gui_mod.messagebox
    gui_mod.messagebox = FakeBox()
    root = tk.Tk()
    root.withdraw()
    app = gui_mod.LightstickPlayerApp(root)
    try:
        app.transport_var.set("BLE 蓝牙")
        spec = app._transport_spec()
        check(spec.startswith("ble"), "界面能拼出 ble 描述串: %s" % spec)

        app.toggle_connect()
        deadline = time.time() + 35.0
        while time.time() < deadline and app.tx is None and not popups:
            root.update()
            time.sleep(0.05)

        check(not popups, "GUI 连接没弹错误框: %s" % (popups[:1],))
        check(app.tx is not None and app.tx.kind == "ble",
              "GUI 连上了 BLE (tx=%s)" % (app.tx.describe() if app.tx else None))

        if app.tx is not None:
            app.toggle_connect()
            root.update()
            check(app.tx is None, "GUI 能正常断开")
            check(app.connect_btn.cget("text") == "连接", "断开后按钮回到「连接」")
    finally:
        gui_mod.messagebox = real_messagebox
        try:
            if app.tx is not None:
                app.tx.close()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


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

    # ---- 3) GUI 的连接路径也要走一遍 ----
    # 这里曾经漏传 on_state 给工厂, 一连 BLE 就 TypeError。
    # 光测 transport 层发现不了, 必须真的走 GUI 那条路。
    gui_check()

    print()
    print("%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
