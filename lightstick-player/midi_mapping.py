# SPDX-License-Identifier: GPL-3.0-only
"""Configurable MIDI rules. Pure data/model code; no Tk or hardware dependency."""
from __future__ import annotations
import colorsys
import copy
import json
import math
from pathlib import Path
from protocol import slot_word

EVENTS = ('control_change', 'note_on', 'note_off', 'pitchwheel', 'aftertouch', 'polytouch', 'program_change')
TARGETS = ('hue', 'saturation', 'value', 'master', 'red', 'green', 'blue', 'function',
           'note_color', 'fixed_color', 'pulse', 'blackout', 'resume', 'hold', 'scene')
CONDITIONS = ('always', 'nonzero', 'rising', 'falling')
MODES = ('absolute', 'relative_twos_complement', 'relative_offset', 'relative_sign_magnitude')

def rule(name='新映射', **values):
    result = dict(name=name, enabled=True, event='control_change', channel=-1, number=-1,
                  target='hue', mode='absolute', in_min=0, in_max=127, out_min=0., out_max=1.,
                  invert=False, gamma=1., deadzone=0., step=1/127, color='#ff0000', palette=7,
                  scene='默认', condition='nonzero', threshold=0)
    result.update(values)
    return result

def default_config():
    return {'version': 2, 'device': 'Minilab3 0', 'push_hz': 50, 'burst': 1,
            'fade_ms': 100, 'bit_us': 250, 'settle_frames': 2, 'rules': [
        rule('旋钮1 · 色相', channel=0, number=74),
        rule('旋钮2 · 饱和度', channel=0, number=71, target='saturation'),
        rule('旋钮3 · 亮度', channel=0, number=76, target='value'),
        rule('推子1 · 主亮度', channel=0, number=82, target='master'),
        rule('琴键 · 色轮', event='note_on', channel=0, target='note_color', in_min=48, in_max=72),
    ], 'scenes': {'默认': {'hue': 0., 'saturation': 1., 'value': 1., 'master': 1., 'function': 0}}}

def validate(config):
    config = copy.deepcopy(config)
    if config.get('version') != 2: raise ValueError('映射文件版本必须为 2')
    if not 1 <= int(config.get('push_hz', 50)) <= 200: raise ValueError('推送频率须为1–200')
    config['push_hz'] = int(config.get('push_hz', 50))
    if config.get('burst', 1) not in (1, 2, 3, 6): raise ValueError('D8帧数须为1/2/3/6')
    config['burst']=config.get('burst',1)
    for key, default, low, high in (('fade_ms',100,0,2000),('settle_frames',2,0,6)):
        value=config.get(key,default)
        if type(value) is not int or not low<=value<=high: raise ValueError(f'{key} 须为{low}–{high}整数')
        config[key]=value
    if config.get('bit_us',250) not in (200,250): raise ValueError('符号宽度须为200或250微秒')
    config['bit_us']=config.get('bit_us',250)
    if not isinstance(config.get('rules'), list) or len(config['rules']) > 256:
        raise ValueError('映射表须为列表，最多256条')
    scenes = config.get('scenes', {})
    if not isinstance(scenes, dict): raise ValueError('场景须为对象')
    for name, state in scenes.items():
        if not isinstance(name, str) or not isinstance(state, dict): raise ValueError('场景格式无效')
        for key in ('hue', 'saturation', 'value', 'master'):
            v = float(state.get(key, 1 if key != 'hue' else 0))
            if not math.isfinite(v) or not 0 <= v <= 1: raise ValueError('场景参数须为0–1')
        if type(state.get('function', 0)) is not int or not 0 <= state.get('function', 0) <= 3:
            raise ValueError('场景效果须为0–3')
        scenes[name] = dict(hue=float(state.get('hue', 0)), saturation=float(state.get('saturation', 1)),
                            value=float(state.get('value', 1)), master=float(state.get('master', 1)),
                            function=state.get('function', 0))
    normalized = []
    for original in config['rules']:
        if not isinstance(original, dict): raise ValueError('映射行格式无效')
        r = rule(**original)
        if r['condition'] not in CONDITIONS: raise ValueError('触发条件无效')
        if type(r['enabled']) is not bool or type(r['invert']) is not bool: raise ValueError('启用和反向须为布尔值')
        if r['event'] not in EVENTS or r['target'] not in TARGETS or r['mode'] not in MODES:
            raise ValueError(f"{r['name']}：事件、目标或编码无效")
        if type(r['channel']) is not int or not -1 <= r['channel'] <= 15:
            raise ValueError('通道须为-1（全部）或0–15')
        if type(r['number']) is not int or not -1 <= r['number'] <= 127:
            raise ValueError('编号须为-1（全部）或0–127')
        for key in ('in_min', 'in_max', 'out_min', 'out_max', 'gamma', 'deadzone', 'step', 'threshold'):
            if not math.isfinite(float(r[key])): raise ValueError('范围/曲线参数必须为有限数')
            r[key] = float(r[key])
        if r['in_max'] <= r['in_min'] or not .05 <= r['gamma'] <= 10 or not 0 <= r['deadzone'] < 1 or not 0 < r['step'] <= 1:
            raise ValueError('输入上限须大于下限；gamma .05–10；死区0–1；步长0–1')
        if not 0 <= r['out_min'] <= 1 or not 0 <= r['out_max'] <= 1:
            raise ValueError('输出范围须为0–1（效果将换算为0–3）')
        if type(r['palette']) is not int or not 0 <= r['palette'] <= 15: raise ValueError('调色板码须为0–15')
        try:
            if len(r['color']) != 7 or not r['color'].startswith('#'): raise ValueError()
            int(r['color'][1:], 16)
        except (ValueError, TypeError): raise ValueError('固定色须为 #RRGGBB')
        if r['target'] == 'scene' and r['scene'] not in scenes: raise ValueError('映射引用了不存在的场景')
        normalized.append(r)
    config['rules'] = normalized
    config.setdefault('scenes', {})
    return config

def save_config(path, config):
    config = validate(config)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)

class MappingEngine:
    def __init__(self, config):
        self.config = validate(config)
        self.state = dict(hue=0., saturation=1., value=1., master=1., function=0)
        self.blackout, self.hold = False, False
        self.last = {}
        self.gates = {}

    def rgb(self):
        return (0., 0., 0.) if self.blackout else colorsys.hsv_to_rgb(
            self.state['hue'], self.state['saturation'], self.state['value']*self.state['master'])

    def payload(self):
        fine = [max(0, min(255, int(v*255+.5))) for v in self.rgb()]
        rgb = [(v+8)//17 for v in fine]
        word = 0 if self.blackout else slot_word(int(self.state['function']), *rgb)
        return {'slots': [word]*9, 'rgb8': fine*9}

    def process(self, message):
        m = dict(message)
        if m.get('type') == 'note_on' and m.get('velocity', 0) == 0: m['type'] = 'note_off'
        events = []
        if m.get('type') == 'note_off':
            for i, r in enumerate(self.config['rules']):
                if r['event'] == 'note_on': self.gates[(i, m.get('channel'), m.get('note'))] = False
        for index, r in enumerate(self.config['rules']):
            if not r['enabled'] or r['event'] != m.get('type'): continue
            if r['channel'] != -1 and r['channel'] != m.get('channel'): continue
            number = m.get('control', m.get('note', m.get('program', -1)))
            if r['number'] != -1 and r['number'] != number: continue
            target = r['target']
            raw = m.get('value', m.get('velocity', m.get('pitch', m.get('program', 0))))
            if target == 'note_color':
                raw = m.get('note', -1)
                if not r['in_min'] <= raw <= r['in_max']: continue
            if target in ('pulse', 'blackout', 'resume', 'hold', 'scene', 'fixed_color'):
                gate = (index, m.get('channel'), number)
                active = raw > r['threshold']
                previous = self.gates.get(gate, False)
                self.gates[gate] = active
                condition = r['condition']
                if condition == 'nonzero' and not active: continue
                if condition == 'rising' and not (active and not previous): continue
                if condition == 'falling' and not (previous and not active): continue
            if target == 'blackout':
                self.blackout = True
                events = [{'cmd': 'BLACKOUT', 'args': {}}]
                break  # Blackout is terminal and wins over all later rules.
            if target == 'resume':
                self.blackout = False
                events.append({'cmd': 'SET_STATE', 'args': dict(self.payload(), release_blackout=True)})
                continue
            if target == 'hold': self.hold = not self.hold; continue
            if self.hold or self.blackout: continue
            if target == 'pulse':
                events.append({'cmd': 'TRIGGER', 'args': {'kind': 'pulse', 'palette': r['palette']}})
                continue
            if target == 'scene': self.state = copy.deepcopy(self.config['scenes'][r['scene']]); continue
            if target == 'fixed_color':
                rgb = [int(r['color'][i:i+2], 16)/255 for i in (1, 3, 5)]
                self.state['hue'], self.state['saturation'], self.state['value'] = colorsys.rgb_to_hsv(*rgb)
                continue
            if r['mode'] == 'absolute' or target == 'note_color':
                v = max(0., min(1., (raw-r['in_min'])/(r['in_max']-r['in_min'])))
                if r['invert']: v = 1-v
                v = r['out_min'] + v**r['gamma']*(r['out_max']-r['out_min'])
                if index in self.last and abs(v-self.last[index]) < r['deadzone']: continue
                self.last[index] = v
            else:
                if r['mode'] == 'relative_twos_complement': delta = raw if raw < 64 else raw-128
                elif r['mode'] == 'relative_offset': delta = raw-64
                else: delta = raw if raw < 64 else -(raw-64)
                if r['invert']: delta = -delta
                base = self.state.get(target, 0)
                if target in ('red', 'green', 'blue'):
                    base = colorsys.hsv_to_rgb(self.state['hue'], self.state['saturation'], self.state['value'])[('red', 'green', 'blue').index(target)]
                if target == 'function': base /= 3
                v = base + delta*r['step']
                if target != 'hue' or (r['out_min'], r['out_max']) != (0., 1.):
                    v = max(min(r['out_min'], r['out_max']), min(max(r['out_min'], r['out_max']), v))
            if target == 'note_color':
                # The endpoint is a distinct hue; 25 keys do not repeat the first color.
                self.state['hue'] = v*(r['in_max']-r['in_min'])/(r['in_max']-r['in_min']+1)
                self.state['value'] = m.get('velocity', 127)/127
            elif target == 'function': self.state['function'] = min(3, max(0, int(v*3+.5)))
            elif target in ('red', 'green', 'blue'):
                rgb = list(colorsys.hsv_to_rgb(self.state['hue'], self.state['saturation'], self.state['value']))
                rgb[('red', 'green', 'blue').index(target)] = max(0., min(1., v))
                self.state['hue'], self.state['saturation'], self.state['value'] = colorsys.rgb_to_hsv(*rgb)
            elif target == 'hue': self.state[target] = v % 1
            else: self.state[target] = max(0., min(1., v))
        return events
