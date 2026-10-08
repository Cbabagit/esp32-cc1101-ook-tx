# SPDX-License-Identifier: GPL-3.0-only
"""Windows MIDI input via the OS WinMM API; no compiler-dependent extension."""
from __future__ import annotations
import ctypes
import sys

def decode_short(packed):
    status, d1, d2 = packed & 255, (packed >> 8) & 127, (packed >> 16) & 127
    channel, kind = status & 15, status & 0xF0
    base = {'channel': channel}
    if kind in (0x80, 0x90): base.update(type='note_off' if kind==0x80 else 'note_on', note=d1, velocity=d2)
    elif kind==0xA0: base.update(type='polytouch', note=d1, value=d2)
    elif kind==0xB0: base.update(type='control_change', control=d1, value=d2)
    elif kind==0xC0: base.update(type='program_change', program=d1)
    elif kind==0xD0: base.update(type='aftertouch', value=d1)
    elif kind==0xE0: base.update(type='pitchwheel', pitch=(d1 | d2<<7)-8192)
    else: return None
    return base

def _api():
    from ctypes import wintypes
    class Caps(ctypes.Structure):
        _fields_ = [('manufacturer', wintypes.WORD), ('product', wintypes.WORD),
                    ('version', wintypes.DWORD), ('name', wintypes.WCHAR*32), ('support', wintypes.DWORD)]
    dll = ctypes.WinDLL('winmm')
    dll.midiInGetNumDevs.restype = wintypes.UINT
    dll.midiInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(Caps), wintypes.UINT]
    dll.midiInGetDevCapsW.restype = wintypes.UINT
    dll.midiInOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT,
                              ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    dll.midiInOpen.restype = wintypes.UINT
    for name in ('midiInStart', 'midiInStop', 'midiInReset', 'midiInClose'):
        function = getattr(dll,name); function.argtypes=[ctypes.c_void_p]; function.restype=wintypes.UINT
    dll.midiInGetErrorTextW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    return dll, Caps

def get_input_names():
    if sys.platform!='win32':
        import mido
        mido.set_backend('mido.backends.rtmidi')
        return mido.get_input_names()
    api,caps_type=_api()
    names=[]
    for index in range(api.midiInGetNumDevs()):
        caps=caps_type()
        if api.midiInGetDevCapsW(index,ctypes.byref(caps),ctypes.sizeof(caps))==0:
            names.append(f'{caps.name} {index}')
    return names

class WindowsSource:
    def __init__(self,name,callback):
        self.api,_=_api()
        self.handle=ctypes.c_void_p()
        self.callback=callback
        self.closed=False
        names=get_input_names()
        if name not in names:raise ValueError('MIDI设备已断开或名称已改变，请刷新设备')
        # Keep this function pointer alive until midiInClose completes.
        signature=ctypes.WINFUNCTYPE(None,ctypes.c_void_p,ctypes.c_uint,
                                     ctypes.c_size_t,ctypes.c_size_t,ctypes.c_size_t)
        def receive(handle,event,instance,param1,param2):
            if not self.closed and event==0x3C3:  # MIM_DATA
                message=decode_short(param1)
                if message is not None:
                    try:self.callback(message)
                    except Exception:pass  # ctypes callbacks must never unwind into WinMM.
        self.native_callback=signature(receive)
        pointer=ctypes.cast(self.native_callback,ctypes.c_void_p).value
        self.check(self.api.midiInOpen(ctypes.byref(self.handle),int(name.rsplit(' ',1)[1]),pointer,0,0x30000))
        try:self.check(self.api.midiInStart(self.handle))
        except Exception:
            self.api.midiInClose(self.handle);raise
    def check(self,result):
        if result:
            text=ctypes.create_unicode_buffer(256)
            self.api.midiInGetErrorTextW(result,text,256)
            raise RuntimeError(text.value or f'WinMM error {result}')
    def close(self):
        if self.closed:return
        self.closed=True
        self.api.midiInStop(self.handle)
        self.api.midiInReset(self.handle)
        self.check(self.api.midiInClose(self.handle))

def open_input(name,callback):
    if sys.platform=='win32':return WindowsSource(name,callback)
    import mido
    mido.set_backend('mido.backends.rtmidi')
    return mido.open_input(name,callback=callback)
