# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
"""传输层测试: 用假板子在本地起一个 UDP 服务, 验证发现/发送/收响应。

真机 UDP 需要板子连上同一个 Wi-Fi; 这里用回环把传输逻辑本身测掉,
不需要任何硬件。
"""
import json
import os
import socket
import sys
import threading
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


class FakeBoard(threading.Thread):
    """最小化的假板子: 回发现请求, 把命令原样回一个 ok。"""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.commands = []

    def run(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                # Windows 上给已经关闭的端口回包会收到 ICMP port unreachable,
                # 下一次 recvfrom 就抛 WSAECONNRESET。服务端不能因为一次
                # 瞬时错误就退出 —— 这正是之前这个测试偶发失败的原因。
                time.sleep(0.01)
                continue
            text = data.decode("utf-8", "replace").strip()
            if text == transport.UDP_DISCOVER_REQUEST.decode():
                self.sock.sendto(json.dumps({
                    "device": "FakeBoard", "firmware_version": "test",
                    "ip": "127.0.0.1", "hostname": "fake", "http_port": 80,
                }).encode(), addr)
                continue
            try:
                command = json.loads(text)
            except ValueError:
                continue
            self.commands.append(command)
            reply = {"id": command.get("id", ""), "ok": True,
                     "result": {"status": "accepted", "echo_cmd": command.get("cmd", "")}}
            self.sock.sendto((json.dumps(reply) + chr(10)).encode(), addr)

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


def main():
    board = FakeBoard(transport.UDP_DISCOVERY_PORT)
    board.start()
    time.sleep(0.5)

    # ---- 1) 广播发现 ----
    info = transport.discover(timeout=2.0)
    check(info is not None, "广播发现收到回应")
    if info:
        check(info.get("device") == "FakeBoard", "发现结果里有 device 字段")

    # ---- 2) UdpTransport: 指定地址 ----
    replies = []
    tx = transport.UdpTransport(host="127.0.0.1", log=lambda s: None,
                                on_reply=replies.append)
    check(tx.describe().startswith("udp 127.0.0.1"), "describe() 带地址")
    tx.send_json({"id": "t1", "cmd": "GET_INFO"})
    deadline = time.time() + 3.0
    while time.time() < deadline and not replies:
        time.sleep(0.02)
    check(len(replies) == 1, "收到 1 条响应")
    if replies:
        check(replies[0].get("id") == "t1", "响应 id 对得上")
        check(replies[0].get("result", {}).get("echo_cmd") == "GET_INFO", "响应内容正确")
    check(len(board.commands) == 1 and board.commands[0]["cmd"] == "GET_INFO",
          "假板子收到了命令")

    # ---- 3) 只保留最新一条 (和串口一致的合帧语义) ----
    board.commands.clear()
    replies.clear()
    for i in range(20):
        tx.send_json({"id": "burst%d" % i, "cmd": "TX_D8"})
    time.sleep(0.4)
    check(len(board.commands) < 20,
          "连发 20 条只送出去 %d 条 (合帧生效)" % len(board.commands))
    tx.close()

    # ---- 4) 超长命令被丢弃 ----
    tx2 = transport.UdpTransport(host="127.0.0.1", log=lambda s: None)
    board.commands.clear()
    tx2.send_json({"id": "big", "cmd": "TX_PULSES",
                   "args": {"durations_us": [250] * 900}})
    time.sleep(0.3)
    check(len(board.commands) == 0, "超过 %d 字节的命令被丢弃" % transport.UDP_MAX_COMMAND)
    tx2.close()

    # ---- 5) 传错地址要在连接时就报错, 而不是每帧刷 getaddrinfo failed ----
    # 这是实际踩过的坑: GUI 里 UDP 地址和 BLE 设备名共用一个输入框, 扫完 BLE
    # 再切到 UDP, 就把 "Lightstick N16R8" 当 IP 发出去了。
    try:
        transport.open_transport("udp:Lightstick N16R8")
        check(False, "非法 UDP 地址应当抛异常")
    except RuntimeError as exc:
        check("无法解析" in str(exc), "非法 UDP 地址给出可读的错误: %s" % str(exc)[:60])
    except Exception as exc:
        check(False, "非法 UDP 地址抛了意外的异常类型: %r" % exc)

    # ---- 6) 同一个错误只报一次 (播放时 20 次/秒的刷屏就是这里来的) ----
    reporter = transport._Reporter(interval=60.0)
    import io
    import contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        for _ in range(50):
            reporter.report("同一个错误")
        reporter.report("另一个错误")
    lines = [l for l in buffer.getvalue().splitlines() if l.strip()]
    check(len(lines) == 2, "重复错误被去重 (50 次 -> %d 行)" % len(lines))

    # ---- 7) 工厂描述串 ----
    dry = transport.open_transport("dry")
    check(dry.kind == "dry", "open_transport('dry') 返回 DryRunTransport")
    check(transport.LightstickTransport is transport.SerialTransport,
          "LightstickTransport 仍指向 SerialTransport (向后兼容)")
    check(transport.BLE_SERVICE_UUID == "8f7a0001-4c53-4331-9638-53334e313652",
          "BLE UUID 与固件一致")

    board.close()
    print()
    print("%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
