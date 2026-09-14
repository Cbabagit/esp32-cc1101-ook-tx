import serial, sys, time
for attempt in range(3):
    try:
        s = serial.Serial("COM10", 921600, timeout=1, write_timeout=1)
        print("OPEN_OK attempt", attempt + 1)
        s.close()
        sys.exit(0)
    except Exception as e:
        print("OPEN_FAIL attempt", attempt + 1, type(e).__name__, e)
        time.sleep(0.5)
sys.exit(1)
