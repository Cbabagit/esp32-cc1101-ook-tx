# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""串口传输层: 对接 Lightstick-Lab 参考固件的 JSON 命令 API (921600 波特率)。

关键设计: 串口写入在后台线程执行, send_json() 只入队(最新一条), 绝不阻塞
调用线程(Tkinter 主线程)。这样 GUI 不会因为串口阻塞而"未响应"。

参考固件(ESP32-S3 + CC1101)通过串口(UART0, 921600)接收 JSON 行命令。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from typing import List, Optional

_NL = "\n"


def list_ports() -> List[str]:
    """返回可用串口设备名列表(如 COM10)。"""
    try:
        import serial.tools.list_ports
        return [p.device for p in serial.tools.list_ports.comports()]
    except Exception:
        return []


class LightstickTransport:
    """JSON 命令串口发送(默认 921600), 后台线程写, 只保留最新命令。"""

    def __init__(self, port: str, baud: int = 921600, log=None):
        import serial
        self._log = log or (lambda s: None)
        # write_timeout=2: 串口写阻塞超过 2 秒就抛异常, 不会无限卡死
        self.ser = serial.Serial(port, baud, timeout=0.2, write_timeout=2)
        self.ser.reset_input_buffer()

        self._latest: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def send_json(self, obj: dict) -> None:
        """入队一条命令(只保留最新一条, 旧的丢弃)。非阻塞, 立即返回。"""
        line = json.dumps(obj, separators=(",", ":"))
        with self._lock:
            self._latest = line

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                line = self._latest
                self._latest = None
            if line is None:
                time.sleep(0.01)
                continue
            try:
                self.ser.write((line + _NL).encode("ascii"))
            except Exception as exc:
                # 写失败(串口忙/超时/已拔出): 打印到 stderr, 不打断线程
                print(f"[transport] write error: {exc}", file=sys.stderr)

    def close(self) -> None:
        self._stop.set()
        try:
            self.ser.close()
        except Exception:
            pass


class DryRunTransport:
    """不接硬件, 只打印命令(用于测试/预览)。"""

    def __init__(self, log=None):
        self._log = log or print

    def send_json(self, obj: dict) -> None:
        self._log(f"[dry-run] {json.dumps(obj, separators=(',',':'))}")

    def close(self) -> None:
        pass
