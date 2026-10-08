# SPDX-License-Identifier: GPL-3.0-only
import json
import threading
import time
import copy

class JsonLink:
    def __init__(self, port='COM10', timeout=3, serial_device=None):
        if serial_device is None:
            import serial
            serial_device = serial.Serial(port, 921600, timeout=0.05, write_timeout=2)
        self.serial = serial_device
        self.timeout = timeout
        self.counter = 0
        self.invalid_replies = []

    def request(self, command, args=None):
        self.counter += 1
        identifier = f'midi-{self.counter}'
        packet = {'id': identifier, 'cmd': command, 'args': args or {}}
        self.serial.write((json.dumps(packet, separators=(',', ':')) + '\n').encode('ascii'))
        deadline = time.monotonic() + self.timeout
        pending = bytearray()
        while time.monotonic() < deadline:
            # Read one byte while idle, then only bytes already buffered. Reading
            # 4096 unconditionally waits for the full serial timeout on every RPC.
            available = getattr(self.serial, 'in_waiting', 0)
            pending.extend(self.serial.read(max(1, min(4096, available))))
            if len(pending) > 65536:
                raise RuntimeError('serial reply buffer exceeded limit')
            while b'\n' in pending:
                raw, _, rest = pending.partition(b'\n')
                pending = bytearray(rest)
                try:
                    reply = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    self.invalid_replies.append(raw.decode('utf-8', 'replace'))
                    self.invalid_replies = self.invalid_replies[-20:]
                    continue
                if not isinstance(reply, dict) or reply.get('id') != identifier:
                    continue
                if not reply.get('ok'):
                    raise RuntimeError(f'{command}: {reply.get("error", reply)}')
                return reply.get('result', {})
        raise TimeoutError(f'{command}: no matching reply; command is NOT retried')

    def close(self):
        self.serial.close()

    def identify(self):
        """Read-only startup probe: a command sent during ESP32 boot may be lost."""
        original = self.timeout
        deadline = time.monotonic() + 10
        try:
            self.timeout = 1
            while True:
                try:
                    return self.request('GET_INFO')
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.1)
        finally:
            self.timeout = original


class Sender:
    """One RPC owner, a latest-state mailbox, and a bounded ordered event queue."""
    def __init__(self, link, push_hz=50):
        if not 1 <= push_hz <= 200:
            raise ValueError('push_hz must be 1..200')
        self.link, self.period = link, 1/push_hz
        self.lock = threading.Lock()
        self.events = []
        self.latest = None
        self.sent = None
        self.latched = False
        self.stop = threading.Event()
        self.error = None
        self.status = {}
        self.dropped_events = 0
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name='midi-rpc', daemon=True)

    def put(self, state, event=None):
        state=copy.deepcopy(state)
        event=copy.deepcopy(event)
        with self.lock:
            if event and event['cmd'] == 'BLACKOUT':
                self.events.clear()
                self.latest = None
                self.latched = True
                self.events.append(event)
            elif event:
                if event['cmd']=='SET_STATE' and event['args'].get('release_blackout'):
                    self.events.clear() # Resume is an explicit control barrier, never dropped.
                if len(self.events) >= 8:
                    self.dropped_events += 1
                    if not self.latched:self.latest=state
                    self.wake.set()
                    return False # Drop the excess action; keep continuous control alive.
                self.events.append(event)
                if event['cmd'] == 'SET_STATE' and event['args'].get('release_blackout'):
                    self.latched = False
                    self.sent = None
            if not self.latched:
                self.latest = state
        self.wake.set()
        return True

    def run(self):
        heartbeat = time.monotonic()
        next_state = heartbeat
        try:
            while not self.stop.is_set():
                with self.lock:
                    event = self.events.pop(0) if self.events else None
                    state = self.latest if not self.latched else None
                if event:
                    try:
                        self.link.request(event['cmd'], event['args'])
                    except RuntimeError as exc:
                        if event['cmd']=='TRIGGER' and 'trigger queue full' in str(exc):
                            self.dropped_events += 1
                        else: raise
                    if event['cmd'] == 'BLACKOUT':
                        self.sent = None
                elif state is not None and state != self.sent and time.monotonic()>=next_state:
                    started=time.monotonic()
                    self.link.request('SET_STATE', state)
                    self.sent = state
                    next_state=started+self.period
                # Status and lease renewal cannot be starved by continuous MIDI input.
                if time.monotonic() - heartbeat >= 0.5:
                    status = self.link.request('GET_ENGINE')
                    self.status = status
                    if not status.get('enabled'):
                        raise RuntimeError(f'engine disabled: {status.get("last_error", "")}')
                    self.link.request('ENGINE', {'enabled': True})
                    heartbeat = time.monotonic()
                delay=self.period
                if state is not None and state != self.sent:
                    delay=max(.001,min(self.period,next_state-time.monotonic()))
                self.wake.wait(delay)
                self.wake.clear()
        except Exception as exc:
            self.error = exc
            self.stop.set()


