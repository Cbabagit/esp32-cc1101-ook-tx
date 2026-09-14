"""发一条 TX_D8 并原样打印固件的每一行响应 (校验帧构造/校验和/脉宽数量)。"""
import json, sys, time
import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
bit_us = int(sys.argv[2]) if len(sys.argv) > 2 else 250
burst = int(sys.argv[3]) if len(sys.argv) > 3 else 6

ser = serial.Serial(port, 921600, timeout=0.3, write_timeout=2)
time.sleep(0.4)
ser.reset_input_buffer()

req = {"id": "probe1", "cmd": "TX_D8", "args": {
    "slots": [0x0F00] * 9, "burst": burst, "bit_us": bit_us, "gap_us": 850,
    "frequency_hz": 433920000, "power_dbm": -20}}
line = json.dumps(req, separators=(",", ":"))
print("TX bytes:", len(line) + 1)
print("TX:", line)
ser.write((line + "\n").encode())
ser.flush()

deadline = time.monotonic() + 8.0
while time.monotonic() < deadline:
    raw = ser.readline()
    if not raw:
        continue
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        continue
    print("RX:", text)
    try:
        obj = json.loads(text)
    except ValueError:
        continue
    if (obj.get("result") or {}).get("status") in ("success", "error"):
        break
ser.close()
