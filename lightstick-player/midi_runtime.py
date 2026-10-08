# SPDX-License-Identifier: GPL-3.0-only
"""MIDI/hardware worker. Never calls Tk; the UI polls bounded data snapshots."""
from __future__ import annotations
import copy
import threading
import time
from midi_mapping import MappingEngine
from midi_link import JsonLink, Sender

class MidiController:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.engine = MappingEngine(config)
        self.source = None
        self.sender = None
        self.worker = None
        self.busy = False
        self.output = False
        self.error = ''
        self.last_message = None
        self.message_sequence = 0
        self.learn_sequence = 0
        self.learnt = None
        self.learning = False
        self.status = {}
        self.dropped_events = 0
        self.stop_event = threading.Event()

    def replace_config(self, config):
        fresh = MappingEngine(config)
        with self.lock:
            fresh.state = copy.deepcopy(self.engine.state)
            fresh.blackout, fresh.hold = self.engine.blackout, self.engine.hold
            self.engine = fresh

    def receive(self, message):
        m = message.dict() if hasattr(message, 'dict') else dict(message)
        with self.lock:
            self.last_message = m
            self.message_sequence += 1
            if self.learning and m.get('type') not in ('note_off', 'clock', 'active_sensing') and not (
                    m.get('type') == 'note_on' and m.get('velocity', 0) == 0):
                self.learnt = copy.deepcopy(m)
                self.learn_sequence += 1
                self.learning = False
                return  # Learning never fires the newly learnt control.
            try:
                events = self.engine.process(m)
                if self.sender:
                    for event in events: self.sender.put(self.engine.payload(), event)
                    self.sender.put(self.engine.payload())
            except Exception as exc:
                self.error = str(exc)
                if self.sender: self.sender.stop.set()

    def connect(self, device):
        if self.busy: return
        self.busy = True
        def job():
            try:
                import midi_io
                source = midi_io.open_input(device, callback=self.receive)
                with self.lock: self.source = source; self.error = ''
            except Exception as exc:
                with self.lock: self.error = str(exc)
            finally: self.busy = False
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def start_output(self, port, config):
        if self.busy or self.output: return
        if self.source is None: raise ValueError('请先连接 MIDI 键盘')
        self.replace_config(config)
        self.busy = True
        self.stop_event.clear()
        def job():
            link, sender = None, None
            try:
                link = JsonLink(port)
                info = link.identify()
                if info.get('version') != '0.5.3-midi2':
                    raise RuntimeError('平滑场控需要0.5.3-midi2固件；当前：'+str(info.get('version')))
                previous = link.request('GET_ENGINE')
                with self.lock:
                    self.engine.blackout = self.engine.blackout or previous.get('blackout_latched', False)
                link.request('ENGINE', {'enabled': True, 'burst': config.get('burst', 1),
                    'fade_ms': config.get('fade_ms',100), 'bit_us': config.get('bit_us',250),
                    'settle_frames': config.get('settle_frames',2)})
                sender = Sender(link, config.get('push_hz', 50))
                with self.lock:
                    self.sender = sender; self.output = True; self.error = ''
                    sender.put(self.engine.payload(), {'cmd': 'BLACKOUT', 'args': {}} if self.engine.blackout else None)
                sender.thread.start()
                self.busy = False
                while not self.stop_event.wait(.1) and not sender.stop.is_set(): pass
                if sender.error: raise RuntimeError(str(sender.error))
            except Exception as exc:
                with self.lock: self.error = str(exc)
            finally:
                with self.lock:
                    if sender:self.dropped_events=sender.dropped_events
                    self.sender = None
                if sender:
                    sender.stop.set()
                    sender.thread.join(6)
                if link:
                    try:
                        if not sender or not sender.thread.is_alive():
                            self.status = link.request('GET_ENGINE')
                            link.request('ENGINE', {'enabled': False})
                            # Stop is graceful: wait for the complete frame and resource cleanup.
                            deadline=time.monotonic()+2
                            while time.monotonic()<deadline:
                                self.status=link.request('GET_ENGINE')
                                if not self.status.get('tx_busy'): break
                                time.sleep(.01)
                    except Exception as exc:
                        with self.lock: self.error = self.error or str(exc)
                    finally: link.close()
                self.output = False; self.busy = False
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def stop_output(self):
        if self.output: self.busy = True
        self.stop_event.set()

    def action(self, name):
        with self.lock:
            if name == 'blackout':
                self.engine.blackout = True
                event = {'cmd': 'BLACKOUT', 'args': {}}
            else:
                self.engine.blackout = False
                event = {'cmd': 'SET_STATE', 'args': dict(self.engine.payload(), release_blackout=True)}
            if self.sender: self.sender.put(self.engine.payload(), event)

    def disconnect(self):
        self.stop_output()
        def job():
            worker = self.worker
            if worker and worker.is_alive(): worker.join(12)
            with self.lock:
                source, self.source = self.source, None
            if source: source.close()
        threading.Thread(target=job, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return {'connected': self.source is not None, 'busy': self.busy, 'output': self.output,
                    'error': self.error, 'last_message': copy.deepcopy(self.last_message),
                    'sequence': self.message_sequence, 'learnt': copy.deepcopy(self.learnt),
                    'learn_sequence': self.learn_sequence, 'rgb': self.engine.rgb(),
                    'state': copy.deepcopy(self.engine.state), 'blackout': self.engine.blackout,
                    'hold': self.engine.hold, 'dropped_events':self.sender.dropped_events if self.sender else self.dropped_events,
                    'status': copy.deepcopy(self.sender.status if self.sender else self.status)}
