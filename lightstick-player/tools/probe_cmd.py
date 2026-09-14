"""发一条任意紧凑命令并打印固件响应 (验证 TX_D8 / TX_BYTES 路径)。"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial

import protocol

port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
what = sys.argv[2] if len(sys.argv) > 2 else "c0"
bit_us = int(sys.argv[3]) if len(sys.argv) > 3 else 250
repeat = int(sys.argv[4]) if len(sys.argv) > 4 else 1
burst = int(sys.argv[5]) if len(sys.argv) > 5 else 1

if what == "d8":
    cmd = protocol.tx_d8_command([(0, 15, 0, 0)] * 9, request_id="p1",
                                 burst_frames=burst, bit_us=bit_us)
    expect_us = protocol.d8_air_time_us(burst, bit_us)
elif what == "c0":
    cmd = protocol.c0_tx_command([0] * 10, request_id="p1", repeat=repeat,
                                 compact=True, bit_us=bit_us)
    expect_us = protocol.tx_air_time_us(protocol.build_c0_frame([0] * 10), repeat, bit_us, 20000)
else:
    cmd = protocol.zone_tx_command(0xFF, 0xFF, 0x01, 0x00, request_id="p1", repeat=repeat,
                                   compact=True, bit_us=bit_us)
    expect_us = protocol.tx_air_time_us(protocol.build_zone_frame(0xFF, 0xFF, 0xFF, 0x01, 0x00),
                                        repeat, bit_us, 20000)

line = json.dumps(cmd, separators=(",", ":"))
print("mode=%s bit_us=%d repeat=%d burst=%d" % (what, bit_us, repeat, burst))
print("TX bytes:", len(line) + 1, " expect_air_us:", expect_us)
print("TX:", line)

ser = serial.Serial(port, 921600, timeout=0.3, write_timeout=2)
time.sleep(0.5)
ser.reset_input_buffer()
ser.write((line + "\n").encode())
ser.flush()

deadline = time.monotonic() + 8.0
while time.monotonic() < deadline:
    raw = ser.readline()
    if not raw:
        continue
    text = raw.decode("utf-8", "replace").strip()
    if not text or "rmt:" in text:
        continue
    print("RX:", text)
    try:
        obj = json.loads(text)
    except ValueError:
        continue
    if (obj.get("result") or {}).get("status") in ("success", "error"):
        break
ser.close()
