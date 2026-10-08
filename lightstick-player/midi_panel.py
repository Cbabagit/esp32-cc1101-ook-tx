# SPDX-License-Identifier: GPL-3.0-only
"""Player-integrated, editable MIDI panel. All Tk work stays on the UI thread."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser, simpledialog
from midi_mapping import rule, default_config, validate, save_config, EVENTS, TARGETS, MODES, CONDITIONS
from midi_runtime import MidiController

EVENT_LABELS = dict(zip(EVENTS, ('CC 控制器', '琴键/垫子按下', '琴键/垫子松开', '弯音轮', '通道压力', '复音压力', '程序切换')))
TARGET_LABELS = dict(zip(TARGETS, ('色相', '饱和度', '亮度', '主亮度', '红色分量', '绿色分量', '蓝色分量',
                                  '闪烁档位', '琴键色轮', '固定颜色', '调色板脉冲', '黑场', '解除黑场', '保持切换', '调用场景')))
CONDITION_LABELS = dict(zip(CONDITIONS, ('每条消息', '大于阈值', '上升沿（按下）', '下降沿（松开）')))
MODE_LABELS = dict(zip(MODES, ('绝对值', '相对：二进制补码', '相对：中心64', '相对：符号/幅度')))

def user_config_path():
    override = os.environ.get('LIGHTSTICK_CONFIG_DIR')
    folder = Path(override) if override else Path(os.environ.get('APPDATA', str(Path.home()))) / 'lightstick-player'
    return folder / 'midi-mapping.json'

class MidiPanel:
    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.path = user_config_path()
        self.config = default_config()
        self.load_error = ''
        if self.path.exists():
            try: self.config = validate(json.loads(self.path.read_text(encoding='utf-8-sig')))
            except Exception as exc: self.load_error = '保存的配置未能读取，已加载默认映射：'+str(exc)
        self.controller = MidiController(self.config)
        self.device = tk.StringVar(value=self.config.get('device', 'Minilab3 0'))
        self.status = tk.StringVar(value='未连接 MIDI 键盘')
        self.monitor = tk.StringVar(value='转动控件或弹奏后，这里会显示真实输入')
        self.learn_index = None
        self.last_learn = 0
        self.last_error = ''
        self.closed = False
        self.last_phase = None
        self.form = {}
        self._build()
        self.refresh_table()
        self.refresh_devices()
        self.parent.after(80, self.poll)

    @property
    def busy(self): return self.controller.busy or self.controller.output

    def _build(self):
        top = ttk.Frame(self.parent, padding=8); top.pack(fill='x')
        ttk.Label(top, text='MIDI 输入').pack(side='left')
        self.device_box = ttk.Combobox(top, textvariable=self.device, width=30, state='readonly')
        self.device_box.pack(side='left', padx=5)
        ttk.Button(top, text='刷新', command=self.refresh_devices).pack(side='left')
        self.connect_button = ttk.Button(top, text='连接键盘', command=self.connect)
        self.connect_button.pack(side='left', padx=5)
        self.start_button = ttk.Button(top, text='启动场控', command=self.start_output)
        self.start_button.pack(side='left', padx=5)
        ttk.Button(top, text='停止场控', command=self.stop_output).pack(side='left')
        ttk.Button(top, text='黑场', command=lambda: self.action('blackout')).pack(side='left', padx=5)
        ttk.Button(top, text='恢复', command=lambda: self.action('resume')).pack(side='left')
        serial_row=ttk.Frame(self.parent,padding=(8,2));serial_row.pack(fill='x')
        ttk.Label(serial_row,text='场控输出串口').pack(side='left')
        self.serial_box=ttk.Combobox(serial_row,textvariable=self.app.port_var,width=12,state='readonly')
        self.serial_box.pack(side='left',padx=5)
        ttk.Button(serial_row,text='刷新串口',command=self.app.refresh_ports).pack(side='left')
        ttk.Label(self.parent, text='先连接键盘即可学习与预览；启动场控使用选择的 USB 串口。全部光棒同步，非分区控制。',
                  padding=(10, 0)).pack(anchor='w')
        bar = ttk.Frame(self.parent, padding=8); bar.pack(fill='x')
        for text, callback in [('新增', self.add), ('复制', self.duplicate), ('删除', self.remove),
                               ('上移', lambda: self.move(-1)), ('下移', lambda: self.move(1)),
                               ('学习选中控件', self.learn), ('保存配置', self.save),
                               ('导入', self.import_file), ('导出', self.export_file), ('默认映射', self.reset)]:
            ttk.Button(bar, text=text, command=callback).pack(side='left', padx=2)
        pane = ttk.Panedwindow(self.parent, orient='horizontal'); pane.pack(fill='both', expand=True, padx=8)
        left = ttk.Frame(pane); pane.add(left, weight=3)
        columns = ('name', 'input', 'channel', 'target', 'curve')
        self.table = ttk.Treeview(left, columns=columns, show='headings', selectmode='browse')
        for key, title, width in zip(columns, ('映射名称', '输入', '通道', '控制目标', '曲线 / 编码'), (170, 115, 50, 100, 140)):
            self.table.heading(key, text=title); self.table.column(key, width=width, minwidth=45)
        scroll = ttk.Scrollbar(left, command=self.table.yview); scroll.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scroll.set); self.table.pack(fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self.select)
        options = ttk.Frame(left); options.pack(fill='x', pady=6)
        ttk.Label(options, text='逐帧冗余数').pack(side='left')
        self.burst = tk.StringVar(value=str(self.config.get('burst', 1)))
        ttk.Combobox(options, textvariable=self.burst, values=('1','2','3','6'), width=4, state='readonly').pack(side='left', padx=4)
        ttk.Label(options, text='推送上限 / 秒').pack(side='left', padx=6)
        self.push = tk.StringVar(value=str(self.config.get('push_hz', 50)))
        ttk.Entry(options, textvariable=self.push, width=5).pack(side='left')
        ttk.Button(options, text='应用输出设置', command=self.apply_options).pack(side='left', padx=5)
        tuning=ttk.Frame(left);tuning.pack(fill='x',pady=3)
        self.fade=tk.StringVar(value=str(self.config.get('fade_ms',100)))
        self.bit=tk.StringVar(value=str(self.config.get('bit_us',250)))
        self.settle=tk.StringVar(value=str(self.config.get('settle_frames',2)))
        ttk.Label(tuning,text='过渡 / 毫秒').pack(side='left')
        ttk.Entry(tuning,textvariable=self.fade,width=5).pack(side='left',padx=4)
        ttk.Label(tuning,text='符号 / 微秒').pack(side='left')
        ttk.Combobox(tuning,textvariable=self.bit,values=('250','200'),state='readonly',width=5).pack(side='left',padx=4)
        ttk.Label(tuning,text='停手后补发帧数').pack(side='left')
        ttk.Combobox(tuning,textvariable=self.settle,values=tuple(str(i) for i in range(7)),state='readonly',width=3).pack(side='left',padx=4)
        ttk.Label(left, text='按表格顺序处理；可让同一控件控制多个参数。黑场优先并锁定，须显式恢复。\n'
                             '琴键色轮的输入范围是音符编号；其他连续映射使用 CC / 力度 / 弯音值。\n'
                             '完整帧之间切换最新状态；停手后补发，普通停止不截断帧。\n'
                             '250µs为兼容档（约7次/秒）；200µs加速档需实棒确认。RGB空口仍为各16级。',
                  wraplength=610, justify='left').pack(anchor='w', pady=5)
        scenes = ttk.Frame(left); scenes.pack(fill='x', pady=5)
        ttk.Label(scenes, text='场景库').pack(side='left')
        self.scene_pick = tk.StringVar(value=next(iter(self.config['scenes']), ''))
        self.scene_box = ttk.Combobox(scenes, textvariable=self.scene_pick, values=list(self.config['scenes']), width=16, state='readonly')
        self.scene_box.pack(side='left', padx=4)
        ttk.Button(scenes, text='当前灯态存为场景', command=self.save_scene).pack(side='left')
        ttk.Button(scenes, text='删除场景', command=self.delete_scene).pack(side='left', padx=4)

        right = ttk.LabelFrame(pane, text='映射编辑', padding=5); pane.add(right, weight=2)
        canvas = tk.Canvas(right, highlightthickness=0, width=345)
        scrollbar = ttk.Scrollbar(right, command=canvas.yview); scrollbar.pack(side='right', fill='y')
        canvas.configure(yscrollcommand=scrollbar.set); canvas.pack(fill='both', expand=True)
        form = ttk.Frame(canvas); window = canvas.create_window((0,0), window=form, anchor='nw')
        form.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        self.enabled = tk.BooleanVar(value=True); self.invert = tk.BooleanVar(value=False)
        flags = ttk.Frame(form); flags.grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Checkbutton(flags, text='启用', variable=self.enabled).pack(side='left')
        ttk.Checkbutton(flags, text='反向', variable=self.invert).pack(side='left', padx=10)
        fields = [('name','名称',None), ('event','输入类型',list(EVENT_LABELS.values())),
                  ('channel','通道', ['全部']+[str(i) for i in range(1,17)]),
                  ('number','CC / 音符编号（-1全部）',None), ('target','控制目标',list(TARGET_LABELS.values())),
                  ('mode','旋钮编码',list(MODE_LABELS.values())), ('in_min','输入下限',None), ('in_max','输入上限',None),
                  ('out_min','输出下限（0–1）',None), ('out_max','输出上限（0–1）',None), ('gamma','Gamma 曲线',None),
                  ('deadzone','死区（0–1）',None), ('step','相对步长（0–1）',None), ('color','固定色 #RRGGBB',None),
                  ('condition','动作触发条件',list(CONDITION_LABELS.values())), ('threshold','动作阈值',None),
                  ('palette','脉冲调色板码（0–15）',None), ('scene','场景名称',None)]
        for row, (key, label, values) in enumerate(fields, 1):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=3, pady=3)
            var = tk.StringVar(); self.form[key] = var
            widget = ttk.Combobox(form, textvariable=var, values=values, state='readonly', width=17) if values else ttk.Entry(form, textvariable=var, width=19)
            widget.grid(row=row, column=1, sticky='ew', padx=3, pady=3)
        form.columnconfigure(1, weight=1)
        buttons = ttk.Frame(form); buttons.grid(row=len(fields)+1, column=0, columnspan=2, sticky='ew', pady=8)
        ttk.Button(buttons, text='选颜色', command=self.choose_color).pack(side='left')
        ttk.Button(buttons, text='应用到选中映射', command=self.apply).pack(side='left', padx=5)
        for widget in form.winfo_children():
            widget.bind('<MouseWheel>', lambda e: canvas.yview_scroll(-int(e.delta/120), 'units'))
        bottom = ttk.LabelFrame(self.parent, text='输入监视与灯光预览', padding=8); bottom.pack(fill='x', padx=8, pady=8)
        self.preview = tk.Label(bottom, text='当前目标', bg='#ff0000', fg='white', width=12, height=2)
        self.preview.pack(side='left', padx=(0,8))
        labels = ttk.Frame(bottom); labels.pack(side='left', fill='x', expand=True)
        ttk.Label(labels, textvariable=self.monitor, wraplength=900).pack(anchor='w')
        ttk.Label(labels, textvariable=self.status, wraplength=900).pack(anchor='w', pady=(4,0))
        self.rf_status=tk.StringVar(value='板端状态将在启动后显示；预览为目标颜色')
        ttk.Label(labels,textvariable=self.rf_status,wraplength=900).pack(anchor='w')
        if self.load_error: self.app._log(self.load_error)

    def selected(self):
        selected = self.table.selection()
        return int(selected[0]) if selected else None

    def refresh_table(self, selected=None):
        self.table.delete(*self.table.get_children())
        for i, r in enumerate(self.config['rules']):
            source = EVENT_LABELS[r['event']]+(' *' if r['number']==-1 else ' '+str(r['number']))
            self.table.insert('', 'end', iid=str(i), values=(('✓ ' if r['enabled'] else '○ ')+r['name'], source,
                '全部' if r['channel']==-1 else r['channel']+1, TARGET_LABELS[r['target']], MODE_LABELS[r['mode']]))
        if self.config['rules']:
            index = min(selected if selected is not None else 0, len(self.config['rules'])-1)
            self.table.selection_set(str(index)); self.select()

    def select(self, *_):
        index = self.selected()
        if index is None: return
        r = self.config['rules'][index]
        self.enabled.set(r['enabled']); self.invert.set(r['invert'])
        for key, var in self.form.items():
            value = r[key]
            if key=='event': value=EVENT_LABELS[value]
            elif key=='target': value=TARGET_LABELS[value]
            elif key=='mode': value=MODE_LABELS[value]
            elif key=='condition': value=CONDITION_LABELS[value]
            elif key=='channel': value='全部' if value==-1 else str(value+1)
            var.set(str(value))

    def commit(self, candidate, selected=None):
        try:
            candidate = validate(candidate)
            if self.busy and any(candidate[key] != self.config.get(key) for key in ('burst','push_hz','fade_ms','bit_us','settle_frames')):
                raise ValueError('先停止场控，再更改或导入不同的输出设置')
            self.controller.replace_config(candidate)
            self.controller.learning=False;self.learn_index=None
            self.config = candidate
            self.push.set(str(candidate['push_hz'])); self.burst.set(str(candidate['burst']))
            self.fade.set(str(candidate['fade_ms']));self.bit.set(str(candidate['bit_us']));self.settle.set(str(candidate['settle_frames']))
            self.refresh_table(selected)
            self.scene_box['values'] = list(self.config['scenes'])
            self.status.set('映射已应用；保存配置后下次启动仍保留')
            return True
        except Exception as exc:
            messagebox.showerror('MIDI 映射', str(exc), parent=self.parent)
            return False

    def apply(self):
        index = self.selected()
        if index is None: return
        try:
            r = rule()
            for key, var in self.form.items():
                value = var.get()
                if key=='event': value=next(k for k,v in EVENT_LABELS.items() if v==value)
                elif key=='target': value=next(k for k,v in TARGET_LABELS.items() if v==value)
                elif key=='mode': value=next(k for k,v in MODE_LABELS.items() if v==value)
                elif key=='condition': value=next(k for k,v in CONDITION_LABELS.items() if v==value)
                elif key=='channel': value=-1 if value=='全部' else int(value)-1
                elif key in ('number','palette'): value=int(value)
                elif key in ('in_min','in_max','out_min','out_max','gamma','deadzone','step','threshold'): value=float(value)
                r[key]=value
            r['enabled'],r['invert']=self.enabled.get(),self.invert.get()
            candidate=copy.deepcopy(self.config); candidate['rules'][index]=r
            self.commit(candidate,index)
        except Exception as exc: messagebox.showerror('MIDI 映射', str(exc), parent=self.parent)

    def add(self):
        c=copy.deepcopy(self.config); c['rules'].append(rule(enabled=False)); self.commit(c,len(c['rules'])-1)
    def duplicate(self):
        index=self.selected()
        if index is None: return
        c=copy.deepcopy(self.config); r=copy.deepcopy(c['rules'][index]); r['name']+=' 副本'; c['rules'].insert(index+1,r)
        self.commit(c,index+1)
    def remove(self):
        index=self.selected()
        if index is None: return
        c=copy.deepcopy(self.config); c['rules'].pop(index); self.commit(c,index)
    def move(self,delta):
        index=self.selected()
        if index is None or not 0<=index+delta<len(self.config['rules']): return
        c=copy.deepcopy(self.config); c['rules'][index],c['rules'][index+delta]=c['rules'][index+delta],c['rules'][index]
        self.commit(c,index+delta)
    def learn(self):
        if not self.controller.source: self.status.set('先连接键盘，再学习控件'); return
        self.learn_index=self.selected()
        if self.learn_index is None: return
        self.controller.learning=True
        self.status.set('学习中：转动或按下要绑定的控件；该次输入只用于学习')
    def choose_color(self):
        result=colorchooser.askcolor(self.form['color'].get(),parent=self.parent)
        if result[1]: self.form['color'].set(result[1])
    def save(self):
        self.config['device']=self.device.get()
        try: save_config(self.path,self.config); self.status.set('已保存配置：'+str(self.path))
        except Exception as exc: messagebox.showerror('保存失败',str(exc),parent=self.parent)
    def import_file(self):
        path=filedialog.askopenfilename(parent=self.parent,filetypes=[('MIDI映射','*.json')])
        if not path:return
        try:
            candidate=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            if self.commit(candidate):
                self.device.set(self.config.get('device','')); self.push.set(str(self.config['push_hz'])); self.burst.set(str(self.config['burst']))
        except Exception as exc: messagebox.showerror('导入失败',str(exc),parent=self.parent)
    def export_file(self):
        path=filedialog.asksaveasfilename(parent=self.parent,defaultextension='.json',initialfile='midi-mapping.json')
        if path:
            try: save_config(path,self.config)
            except Exception as exc: messagebox.showerror('导出失败',str(exc),parent=self.parent)
    def reset(self):
        if messagebox.askyesno('默认映射','替换当前映射为本次实测的 MiniLab 3 默认映射？',parent=self.parent): self.commit(default_config())
    def save_scene(self):
        name=simpledialog.askstring('保存场景','场景名称',parent=self.parent)
        if not name:return
        c=copy.deepcopy(self.config); c['scenes'][name]=self.controller.snapshot()['state']; self.commit(c)
        self.scene_pick.set(name)
    def delete_scene(self):
        name=self.scene_pick.get()
        if any(r['target']=='scene' and r['scene']==name for r in self.config['rules']):
            messagebox.showwarning('场景仍在使用','先修改引用此场景的映射。',parent=self.parent); return
        c=copy.deepcopy(self.config); c['scenes'].pop(name,None); self.commit(c)
    def apply_options(self):
        if self.busy: self.status.set('先停止场控，再更改帧数和推送频率'); return
        try:
            c=copy.deepcopy(self.config); c['burst']=int(self.burst.get()); c['push_hz']=int(self.push.get())
            c['fade_ms']=int(self.fade.get());c['bit_us']=int(self.bit.get());c['settle_frames']=int(self.settle.get())
            self.commit(c,self.selected())
        except Exception as exc: messagebox.showerror('输出设置',str(exc),parent=self.parent)
    def refresh_devices(self):
        def job():
            try:
                import midi_io
                ports=midi_io.get_input_names()
                self.app._post(lambda: self._devices(ports))
            except Exception as exc: self.app._post(lambda error=str(exc): self.status.set('MIDI不可用：'+error))
        threading.Thread(target=job,daemon=True).start()
    def _devices(self,ports):
        if self.closed:return
        self.device_box['values']=ports
        if self.device.get() not in ports: self.device.set(ports[0] if ports else '')
    def connect(self):
        if self.controller.source:
            self.controller.disconnect(); return
        if not self.device.get(): self.status.set('未发现 MIDI 输入设备'); return
        self.controller.connect(self.device.get())
    def start_output(self):
        if self.busy:return
        if str(self.app.connect_btn['state']) == 'disabled':
            self.status.set('播放器正在连接，等待连接完成后再启动场控');return
        if not self.controller.source: self.status.set('请先连接 MIDI 键盘'); return
        if self.app.transport_var.get()!=self.app.TRANSPORTS[0]:
            messagebox.showwarning('MIDI 场控','当前 MIDI 场控使用 USB 串口，请在播放器页选择 USB 串口。',parent=self.parent);return
        port=self.app.port_var.get().strip()
        if not port:self.status.set('先在播放器页选择串口，例如 COM10');return
        self.app.stop()
        if self.app.tx is not None:
            self.app.tx.close();self.app.tx=None
        self.app.connect_btn.config(text='连接');self.app.conn_state.config(text='MIDI 使用 '+port)
        try:self.controller.start_output(port,copy.deepcopy(self.config))
        except Exception as exc:self.status.set(str(exc))
    def stop_output(self):self.controller.stop_output()
    def action(self,name):
        try:self.controller.action(name)
        except Exception as exc:self.status.set(str(exc))
    def poll(self):
        if self.closed:return
        s=self.controller.snapshot()
        rf=s['status']
        if rf:
            self.rf_status.set(f"完整状态帧 {rf.get('state_frames_completed',0)} · 合并 {rf.get('coalesced',0)} · "
                f"中断 {rf.get('frames_aborted',0)} · 错误 {rf.get('failures',0)} · "
                f"PC丢弃动作 {s['dropped_events']} / 板端 {rf.get('dropped_triggers',0)} · "
                f"{'过渡中' if rf.get('ramping') else '目标稳定'}")
        self.device_box.config(state='disabled' if s['busy'] or s['connected'] else 'readonly')
        self.serial_box.config(state='disabled' if s['busy'] or s['output'] else 'readonly')
        self.connect_button.config(text='断开键盘' if s['connected'] else '连接键盘',state='disabled' if s['busy'] else 'normal')
        self.start_button.config(state='disabled' if s['busy'] or s['output'] else 'normal')
        if s['last_message']: self.monitor.set(f"输入 {s['sequence']} 条 · {s['last_message']}")
        rgb=[int(v*255+.5) for v in s['rgb']]; color='#%02x%02x%02x'%tuple(rgb)
        self.preview.config(bg=color,text='黑场锁定' if s['blackout'] else '保持' if s['hold'] else color,
                            fg='black' if sum(rgb)>400 else 'white')
        if s['error']:
            self.status.set('场控错误：'+s['error'])
            if s['error']!=self.last_error:self.app._log('MIDI：'+s['error']);self.last_error=s['error']
        else:
            phase=(s['busy'],s['output'],s['connected'])
            if phase != self.last_phase:
                if s['busy']: self.status.set('连接 / 停止处理中…')
                elif s['output']: self.status.set('实时场控中 · MIDI → 串口 → 固件 · 预览为目标状态')
                else:
                    self.status.set('键盘已连接，可学习与预览' if s['connected'] else '未连接 MIDI 键盘')
                    if self.app.tx is None: self.app.conn_state.config(text='未连接')
            self.last_phase=phase
        if s['learn_sequence']!=self.last_learn and self.learn_index is not None:
            self.last_learn=s['learn_sequence'];m=s['learnt'];index=self.learn_index;self.learn_index=None
            if index<len(self.config['rules']):
                c=copy.deepcopy(self.config);r=c['rules'][index];r['event']=m['type'];r['channel']=m.get('channel',-1)
                r['number']=m.get('control',m.get('note',m.get('program',-1)))
                if r['target']=='note_color':r['number']=-1
                if m['type']=='pitchwheel':r['in_min'],r['in_max']=-8192,8191
                self.commit(c,index);self.status.set('已学习实际控件，范围与控制目标仍可编辑')
        self.parent.after(80,self.poll)
    def close(self):
        self.closed=True;self.controller.disconnect()
