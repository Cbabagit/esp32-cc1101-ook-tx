"""TX_D8 实测工具: 扫 bit_us / burst, 测量端到端耗时并观察棒子是否响应。

用法: py tools/d8_sweep.py --port COM10 --burst 1 --bit-us 250 200 150 100 75 60
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import serial

FREQ_HZ = 433_920_000
POWER_DBM = -20


def slots_red(n: int = 9):
    """func=0(常亮) r=15 g=0 b=0 -> 0x0F00, 9 个通道全红。"""
    return [0x0F00] * n


def slots_off(n: int = 9):
    return [0x0000] * n


def send(ser: serial.Serial, payload: dict) -> None:
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    ser.write(line.encode("ascii"))
    ser.flush()


def read_until(ser: serial.Serial, request_id: str, deadline_s: float):
    """读到 id 匹配的异步结果为止, 返回 (result_dict, lines_seen, waited_ms)。"""
    started = time.monotonic()
    seen = []
    while time.monotonic() - started < deadline_s:
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        seen.append(text)
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if obj.get("id") != request_id:
            continue
        result = obj.get("result") or {}
        if result.get("status") in ("success", "error"):
            return result, seen, int((time.monotonic() - started) * 1000)
    return None, seen, int((time.monotonic() - started) * 1000)


def make_request(request_id: str, slots, burst: int, bit_us: int, gap_us: int, power_dbm: int) -> dict:
    return {
        "id": request_id,
        "cmd": "TX_D8",
        "args": {
            "slots": slots,
            "burst": burst,
            "bit_us": bit_us,
            "gap_us": gap_us,
            "frequency_hz": FREQ_HZ,
            "power_dbm": power_dbm,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM10")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--burst", type=int, default=1)
    parser.add_argument("--gap-us", type=int, default=850)
    parser.add_argument("--power-dbm", type=int, default=POWER_DBM)
    parser.add_argument("--bit-us", type=int, nargs="+", default=[250])
    parser.add_argument("--hold-ms", type=int, default=2500,
                        help="每档颜色保持时间, 便于肉眼/录像观察")
    parser.add_argument("--off-between", action="store_true",
                        help="每档之间发一次全灭, 便于分辨是哪一档生效")
    args = parser.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.2, write_timeout=2)
    time.sleep(0.3)
    ser.reset_input_buffer()

    print("port=%s baud=%d burst=%d gap_us=%d power=%ddBm"
          % (args.port, args.baud, args.burst, args.gap_us, args.power_dbm))
    print("%-5s %-8s %-10s %-10s %-9s %s" % ("step", "bit_us", "air_ms", "rtt_ms", "pulses", "note"))

    for index, bit_us in enumerate(args.bit_us):
        air_ms = (521 * bit_us * args.burst + args.gap_us * (args.burst - 1)) / 1000.0
        request_id = "sweep-%d" % index
        send(ser, make_request(request_id, slots_red(), args.burst, bit_us, args.gap_us, args.power_dbm))
        result, seen, waited = read_until(ser, request_id, 6.0)
        if result is None:
            note = "NO_RESULT " + " | ".join(seen[-2:])[:90]
            pulses = "-"
        elif result.get("status") == "error":
            note = "ERROR " + str(result.get("error"))[:80]
            pulses = "-"
        else:
            pulses = str(result.get("pulse_count"))
            note = "ok"
        print("%-5d %-8d %-10.1f %-10d %-9s %s" % (index + 1, bit_us, air_ms, waited, pulses, note))
        sys.stdout.flush()
        time.sleep(args.hold_ms / 1000.0)
        if args.off_between:
            request_id = "off-%d" % index
            send(ser, make_request(request_id, slots_off(), args.burst, bit_us, args.gap_us, args.power_dbm))
            read_until(ser, request_id, 6.0)
            time.sleep(0.4)

    ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
