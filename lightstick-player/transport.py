# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-player contributors
#
# 本文件是 esp32-cc1101-ook-tx 的 lightstick-player 部分, 以 GPL-3.0-only 发布。
# 固件部分派生自 Lightstick-Lab, 见仓库根目录 THIRD_PARTY_NOTICES.md。
"""传输层: 串口 / Wi-Fi UDP / BLE, 三者对上层是同一套接口。

板子三种接口收的都是同一套换行分隔的 JSON 命令, 只是承载不同:

| 传输 | 承载 | 说明 |
| --- | --- | --- |
| 串口 | UART0 921600 8N1 | 最稳, 需要插线 |
| Wi-Fi UDP | UDP 4210 | 一个数据包一条命令, 板子把结果回给来源端口 |
| BLE | GATT | 写入命令特征, 响应走通知特征 |

三种都实现同一个接口:

- send_json(obj)  发一条命令, 非阻塞 (后台线程写, 只保留最新一条)
- close()         收尾

UDP 端口同时也是发现端口: 广播 LIGHTSTICK_DISCOVER 会收到设备信息 (ip/hostname/端口),
所以 udp 传输可以在不知道 IP 的情况下自动找到板子。发现是重发式的 —— 单发一次丢包
就直接失败, 而且广播可能被机器上的代理/TUN 虚拟网卡吃掉, 所以同时往广播地址和回环发。

Windows 上的一个坑: 给已经关闭的 UDP 端口回包会收到 ICMP port unreachable, 下一次
recvfrom 抛 WSAECONNRESET (ConnectionResetError)。收发两侧都不要把它当成致命错误,
否则一次瞬时错误就会让线程静默退出。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from typing import Callable, List, Optional

_NL = chr(10)

# 固件里写死的常量, 改固件时这里要同步 (firmware/src/main.cpp 顶部)
UDP_DISCOVERY_PORT = 4210
UDP_DISCOVER_REQUEST = b"LIGHTSTICK_DISCOVER"
UDP_MAX_COMMAND = 1400
BLE_DEVICE_NAME = "Lightstick N16R8"
BLE_SERVICE_UUID = "8f7a0001-4c53-4331-9638-53334e313652"
BLE_COMMAND_UUID = "8f7a0002-4c53-4331-9638-53334e313652"
BLE_RESPONSE_UUID = "8f7a0003-4c53-4331-9638-53334e313652"


def list_ports() -> List[str]:
    """返回可用串口设备名列表(如 COM10)。"""
    try:
        import serial.tools.list_ports
        return [p.device for p in serial.tools.list_ports.comports()]
    except Exception:
        return []


class _Reporter:
    """同一个错误只报一次, 之后按间隔静音。

    播放时每帧都会发命令, 链路一断就是 20 次/秒的重复报错, 刷屏且掩盖真正的问题。
    """

    def __init__(self, interval: float = 10.0):
        self.interval = interval
        self._last: dict = {}

    def report(self, message: str) -> None:
        now = time.monotonic()
        previous = self._last.get(message)
        if previous is not None and now - previous < self.interval:
            return
        self._last[message] = now
        print("[transport] " + message, file=sys.stderr)

    def reset(self) -> None:
        self._last.clear()


class Transport:
    """传输层公共接口。"""

    kind = "?"

    def send_json(self, obj: dict) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return self.kind


# ---------------------------------------------------------------------------
# 串口
# ---------------------------------------------------------------------------
class SerialTransport(Transport):
    """JSON 命令串口发送(默认 921600), 后台线程写, 只保留最新命令。"""

    kind = "usb"

    def __init__(self, port: str, baud: int = 921600, log=None):
        import serial
        self._log = log or (lambda s: None)
        self.port = port
        self.baud = baud
        # write_timeout=2: 串口写阻塞超过 2 秒就抛异常, 不会无限卡死
        self.ser = serial.Serial(port, baud, timeout=0.2, write_timeout=2)
        self.ser.reset_input_buffer()

        self._latest: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def describe(self) -> str:
        return "usb %s @%d" % (self.port, self.baud)

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
                print("[transport] write error: %s" % exc, file=sys.stderr)

    def close(self) -> None:
        self._stop.set()
        try:
            self.ser.close()
        except Exception:
            pass


# 旧名字, 保持向后兼容
LightstickTransport = SerialTransport


# ---------------------------------------------------------------------------
# Wi-Fi UDP
# ---------------------------------------------------------------------------
# 广播目标: 全局广播 + 回环。回环这一条是为了稳妥 —— 机器上装了代理/TUN 虚拟网卡
# 时, 255.255.255.255 可能被路由到虚拟网卡上, 真正监听的那个 socket 反而收不到;
# UDP 发现丢一包就失败, 所以两个目标都发、而且每个目标重发几次。
DISCOVER_TARGETS = ("255.255.255.255", "127.0.0.1")


def discover(timeout: float = 1.5, port: int = UDP_DISCOVERY_PORT,
             targets=DISCOVER_TARGETS) -> Optional[dict]:
    """广播发现板子, 返回设备信息 dict (含 ip/hostname/http_port), 找不到返回 None。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.1)
    deadline = time.monotonic() + timeout
    # 在超时窗口内按固定间隔重发; 单发一次的话丢包就直接失败
    interval = max(0.15, timeout / 5.0)
    next_send = 0.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                for target in targets:
                    try:
                        sock.sendto(UDP_DISCOVER_REQUEST, (target, port))
                    except OSError:
                        pass
                next_send = now + interval
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return None
            try:
                info = json.loads(data.decode("utf-8", "replace").strip())
            except ValueError:
                continue
            if isinstance(info, dict) and "device" in info:
                info["_addr"] = addr[0]
                return info
        return None
    finally:
        sock.close()


class UdpTransport(Transport):
    """Wi-Fi UDP: 一个数据包一条命令, 板子把结果回给来源端口。

    host 不填就先广播发现。后台线程负责写命令并收响应, send_json 不阻塞。
    """

    kind = "udp"

    def __init__(self, host: Optional[str] = None, port: int = UDP_DISCOVERY_PORT,
                 log=None, on_reply: Optional[Callable[[dict], None]] = None,
                 discover_timeout: float = 1.5):
        self._log = log or (lambda s: None)
        self.on_reply = on_reply
        self.port = port
        self.info: Optional[dict] = None

        if not host:
            self._log("UDP: 广播发现板子...")
            self.info = discover(timeout=discover_timeout, port=port)
            if not self.info:
                raise RuntimeError(
                    "UDP 发现失败: 没收到 %s 的回应。"
                    "确认板子已连上同一个 Wi-Fi, 或直接用 --host 指定 IP。"
                    % UDP_DISCOVER_REQUEST.decode())
            host = self.info.get("ip") or self.info.get("_addr")
            self._log("UDP: 发现 %s (%s)" % (host, self.info.get("hostname", "?")))
        self.host = host
        self._reporter = _Reporter()

        # 地址在这里就解析掉。放给 sendto 的话, 地址写错会变成每帧一次的
        # "getaddrinfo failed", 既看不出原因又刷屏。
        try:
            self._target = (socket.gethostbyname(host), port)
        except OSError as exc:
            raise RuntimeError(
                "UDP 地址无法解析: %r (%s)。--host 要填 IP, 例如 --host 192.168.1.57;"
                " 留空则自动发现。" % (host, exc)) from exc

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind(("", 0))        # 让板子的响应能回到我们这里
        self._sock.settimeout(0.05)

        self._latest: Optional[bytes] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def describe(self) -> str:
        return "udp %s:%d" % (self.host, self.port)

    def send_json(self, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        if len(payload) > UDP_MAX_COMMAND:
            # 固件也只收 1400 字节以内的单包 (避免 IP 分片)
            print("[transport] 命令 %d 字节超过 UDP 上限 %d, 已丢弃; 超长命令请用串口"
                  % (len(payload), UDP_MAX_COMMAND), file=sys.stderr)
            return
        with self._lock:
            self._latest = payload

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                payload = self._latest
                self._latest = None
            if payload is not None:
                try:
                    self._sock.sendto(payload, self._target)
                    self._reporter.reset()
                except Exception as exc:
                    self._reporter.report("udp 发送失败 (目标 %s:%d): %s"
                                          % (self._target[0], self._target[1], exc))
            self._drain_replies()
            time.sleep(0.005)

    def _drain_replies(self) -> None:
        while True:
            try:
                data, _addr = self._sock.recvfrom(4096)
            except socket.timeout:
                return
            except OSError:
                return
            for line in data.decode("utf-8", "replace").split(chr(10)):
                line = line.strip()
                if not line:
                    continue
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if self.on_reply is not None:
                    self.on_reply(reply)

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# BLE (GATT)
# ---------------------------------------------------------------------------
class BleTransport(Transport):
    """BLE: 往命令特征写换行分隔的 JSON, 响应从通知特征读回来。

    需要 bleak。连接在后台事件循环里跑, __init__ 会阻塞到连上或超时。
    """

    kind = "ble"

    def __init__(self, name: Optional[str] = None, address: Optional[str] = None,
                 log=None, on_reply: Optional[Callable[[dict], None]] = None,
                 on_state: Optional[Callable[[bool, str], None]] = None,
                 scan_timeout: float = 10.0, connect_timeout: float = 25.0):
        import asyncio
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise RuntimeError("BLE 传输需要 bleak: pip install bleak") from exc

        self._asyncio = asyncio
        self._BleakClient = BleakClient
        self._BleakScanner = BleakScanner
        self._log = log or (lambda s: None)
        self.on_reply = on_reply
        self.on_state = on_state
        self.name = name or BLE_DEVICE_NAME
        self.address = address
        self.device_label = address or self.name
        self._scan_timeout = scan_timeout
        self._reporter = _Reporter()
        self._connected = False

        self._latest: Optional[bytes] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._client = None
        self._chunks: List[str] = []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        if not self._ready.wait(connect_timeout):
            self._stop.set()
            raise RuntimeError("BLE 连接超时: %s" % self.device_label)
        if self._error is not None:
            raise RuntimeError(self._error)

    def describe(self) -> str:
        return "ble %s" % self.device_label

    # ---- 后台事件循环 ----
    def _run(self) -> None:
        self._asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            self._error = "BLE 失败: %s" % exc
            self._ready.set()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _find_device(self) -> str:
        if self.address:
            return self.address
        self._log("BLE: 扫描 %s ..." % self.name)
        devices = await self._BleakScanner.discover(timeout=self._scan_timeout)
        fallback = None
        for device in devices:
            device_name = getattr(device, "name", None) or ""
            if device_name == self.name:
                return device.address
            if fallback is None and self.name.lower() in device_name.lower():
                fallback = device.address
        if fallback:
            return fallback
        raise RuntimeError(
            "没扫到 BLE 设备 %r (扫到: %s)。板子的 BLE 名字见 firmware 里的 BLE_DEVICE_NAME。"
            % (self.name, [getattr(d, "name", None) for d in devices][:8]))

    async def _main(self) -> None:
        target = await self._find_device()
        self.device_label = target
        self._log("BLE: 连接 %s ..." % target)
        client = self._BleakClient(target, disconnected_callback=self._on_disconnect)
        await client.connect()
        self._client = client
        await client.start_notify(BLE_RESPONSE_UUID, self._on_notify)
        self._connected = True
        self._reporter.reset()
        self._ready.set()
        try:
            await self._writer(client)
        finally:
            try:
                await client.stop_notify(BLE_RESPONSE_UUID)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass

    def _on_disconnect(self, _client=None) -> None:
        """链路断了。只通知一次, 并停掉后续写入 —— 否则每帧都会报一次
        "Not connected", 播放时就是 20 次/秒。"""
        if self._connected:
            self._connected = False
        if self.on_state is not None:
            self.on_state(False, self.device_label)

    def is_connected(self) -> bool:
        client = self._client
        if client is None:
            return False
        return self._connected and bool(getattr(client, "is_connected", False))

    async def _writer(self, client) -> None:
        while not self._stop.is_set():
            with self._lock:
                payload = self._latest
                self._latest = None
            if payload is None:
                await self._asyncio.sleep(0.005)
                continue
            if not self.is_connected():
                # 断线时丢掉待发命令: 不留积压, 也不刷错误
                continue
            try:
                await client.write_gatt_char(BLE_COMMAND_UUID, payload, response=False)
                self._reporter.reset()
            except Exception as exc:
                self._reporter.report("ble 写入失败: %s" % exc)
                if not getattr(client, "is_connected", False):
                    self._on_disconnect(client)

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        """固件按 180 字节分片发通知, 用换行做帧界, 这里拼回来。"""
        self._chunks.append(bytes(data).decode("utf-8", "replace"))
        joined = "".join(self._chunks)
        while chr(10) in joined:
            line, joined = joined.split(chr(10), 1)
            self._chunks = [joined] if joined else []
            line = line.strip()
            if not line:
                continue
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if self.on_reply is not None:
                self.on_reply(reply)

    def send_json(self, obj: dict) -> None:
        # 固件的 onWrite 会一直拼到换行为止, 所以一次写完即可 (BLE 长写会自己分包)
        payload = (json.dumps(obj, separators=(",", ":")) + chr(10)).encode("utf-8")
        if len(payload) > 4096:
            print("[transport] 命令 %d 字节对 BLE 太长, 已丢弃" % len(payload),
                  file=sys.stderr)
            return
        with self._lock:
            self._latest = payload

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 干跑 (不接硬件)
# ---------------------------------------------------------------------------
class DryRunTransport(Transport):
    """不接硬件, 只打印命令(用于测试/预览)。"""

    kind = "dry"

    def __init__(self, log=None):
        self._log = log or print

    def describe(self) -> str:
        return "dry-run"

    def send_json(self, obj: dict) -> None:
        self._log("[dry-run] " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False))

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def open_transport(spec: str, log=None, on_reply=None, on_state=None,
                   baud: int = 921600, timeout: float = 20.0) -> Transport:
    """按描述串建立传输。

    spec 形如:

    - "usb:COM10" / "COM10" / "serial:COM3"  -> 串口
    - "udp"                                   -> Wi-Fi UDP, 广播自动发现
    - "udp:192.168.1.50"                      -> Wi-Fi UDP, 指定 IP
    - "ble"                                   -> BLE, 按默认名字扫描
    - "ble:Lightstick N16R8"                  -> BLE, 按名字扫描
    - "ble-addr:AA:BB:CC:DD:EE:FF"            -> BLE, 直接按地址连
    - "dry"                                   -> 干跑

    on_reply: 收到设备响应时回调 (可选)
    on_state: 链路状态变化时回调 on_state(connected: bool, label: str) (可选,
              目前只有 BLE 会主动报断开)
    """
    spec = (spec or "").strip()
    lowered = spec.lower()

    if lowered in ("dry", "dry-run", "none", ""):
        return DryRunTransport(log=log)

    if lowered.startswith("udp"):
        host = spec.split(":", 1)[1] if ":" in spec else None
        return UdpTransport(host=host or None, log=log, on_reply=on_reply)

    if lowered.startswith("ble-addr:"):
        return BleTransport(address=spec.split(":", 1)[1], log=log, on_reply=on_reply,
                            on_state=on_state)

    if lowered.startswith("ble"):
        name = spec.split(":", 1)[1] if ":" in spec else None
        return BleTransport(name=name or None, log=log, on_reply=on_reply,
                            on_state=on_state)

    if lowered.startswith("serial:"):
        return SerialTransport(spec.split(":", 1)[1], baud, log=log)

    if lowered.startswith("usb:"):
        return SerialTransport(spec.split(":", 1)[1], baud, log=log)

    return SerialTransport(spec, baud, log=log)