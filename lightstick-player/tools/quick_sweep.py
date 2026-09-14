"""紧凑命令实测: 逐档点亮, 观察荧光棒, 并打印每档的真实往返耗时。"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial
import protocol


def steps_from_args(args):
    out = []
    for spec in args.step:
        kind, repeat, bit_us = spec.split(":")
        repeat, bit_us = int(repeat), int(bit_us)
        if kind == "c0":
            up = protocol.c0_tx_command([0] * 10, request_id="S", repeat=repeat,
                                        compact=True, bit_us=bit_us)
            air = protocol.tx_air_time_us(protocol.build_c0_frame([0] * 10), repeat, bit_us)
            label = "c0 x%d bit%3d" % (repeat, bit_us)
        elif kind == "zone":
            up = protocol.zone_tx_command(0xFF, 0xFF, 0x01, 0x00, request_id="S",
                                          repeat=repeat, compact=True, bit_us=bit_us)
            air = protocol.tx_air_time_us(
                protocol.build_zone_frame(0xFF, 0xFF, 0xFF, 0x01, 0x00), repeat, bit_us)
            label = "zone x%d bit%3d" % (repeat, bit_us)
        else:
            up = protocol.tx_d8_command([(0, 15, 0, 0)] * 9, request_id="S",
                                        burst_frames=repeat, bit_us=bit_us)
            air = protocol.d8_air_time_us(repeat, bit_us)
            label = "d8 x%d bit%3d" % (repeat, bit_us)
        out.append((label, up, air))
    return out


def off_command():
    return protocol.zone_tx_command(0xFF, 0xFF, 0x00, 0x00, request_id="OFF",
                                    repeat=2, compact=True, bit_us=250)


def exchange(ser, cmd, deadline_s=8.0):
    line = json.dumps(cmd, separators=(",", ":"))
    started = time.monotonic()
    ser.write((line + "\n").encode())
    ser.flush()
    while time.monotonic() - started < deadline_s:
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", "replace").strip()
        if not text or "rmt:" in text or "\"event\":\"BOOT\"" in text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        result = obj.get("result") or {}
        if result.get("status") in ("success", "error"):
            return result, int((time.monotonic() - started) * 1000)
    return None, int((time.monotonic() - started) * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--step", nargs="+", default=["c0:1:250", "c0:2:250", "c0:3:250",
                                                  "c0:6:250", "zone:1:250", "d8:1:200"])
    ap.add_argument("--hold-ms", type=int, default=3000)
    ap.add_argument("--cycles", type=int, default=1)
    args = ap.parse_args()

    steps = steps_from_args(args)
    ser = serial.Serial(args.port, 921600, timeout=0.3, write_timeout=2)
    time.sleep(0.6)
    ser.reset_input_buffer()

    print("%-5s %-16s %-9s %-9s %s" % ("step", "command", "air_ms", "rtt_ms", "status"))
    for cycle in range(args.cycles):
        for index, (label, cmd, air) in enumerate(steps):
            cmd = dict(cmd, id="S%d-%d" % (cycle, index))
            result, waited = exchange(ser, cmd)
            status = (result or {}).get("status", "NO_RESULT")
            if status == "error":
                status += " " + str(result.get("error"))[:60]
            print("%-5d %-16s %-9.1f %-9d %s" % (index + 1, label, air / 1000.0, waited, status))
            sys.stdout.flush()
            time.sleep(args.hold_ms / 1000.0)
            exchange(ser, dict(off_command(), id="OFF%d-%d" % (cycle, index)))
            time.sleep(1.0)
    ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
