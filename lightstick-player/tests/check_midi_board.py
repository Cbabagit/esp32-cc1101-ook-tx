# SPDX-License-Identifier: GPL-3.0-only
"""Real WinMM connection + GUI serial handoff + synthesized mapped controls.
This confirms board scheduling, not physical keyboard input or optical reception.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import os
import json
import time
import tempfile
import tkinter as tk
from gui import LightstickPlayerApp
from midi_link import JsonLink

checks=[]
with tempfile.TemporaryDirectory() as tmp:
    os.environ['LIGHTSTICK_CONFIG_DIR']=tmp
    root=tk.Tk();root.withdraw();app=LightstickPlayerApp(root);p=app.midi_panel
    def wait(predicate,limit=15):
        end=time.monotonic()+limit
        while time.monotonic()<end:
            root.update()
            if predicate():return
            if p.controller.error:raise RuntimeError(p.controller.error)
            time.sleep(.03)
        raise TimeoutError(str(p.controller.snapshot()))
    try:
        p.device.set('Minilab3 0');p.connect();wait(lambda:p.controller.source is not None)
        checks.append('real MiniLab 3 WinMM input opened')
        app.port_var.set('COM10');app.toggle_connect();assert app.tx is not None
        checks.append('original CSV serial transport opened')
        p.start_output();wait(lambda:p.controller.output and not p.controller.busy)
        assert app.tx is None
        checks.append('GUI released original serial transport and MIDI acquired COM10')
        wait(lambda:bool(p.controller.snapshot()['status']))
        p.action('resume')
        wait(lambda:not p.controller.snapshot()['status'].get('blackout_latched',True))
        baseline=p.controller.snapshot()['status']['state_frames_completed']
        for value in range(0,128,8):
            p.controller.receive(dict(type='control_change',channel=0,control=74,value=value))
            root.update();time.sleep(.03)
        wait(lambda:p.controller.snapshot()['status'].get('state_frames_completed',0)>baseline)
        initial=p.controller.snapshot()['status'];assert initial['enabled']
        assert initial['state_frames_completed']>0
        checks.append('synthesized CC74 mapped to D8 completed by board')
        p.action('blackout');wait(lambda:p.controller.snapshot()['status'].get('blackout_latched'))
        checks.append('UI blackout acknowledged by board')
        p.action('resume');wait(lambda:not p.controller.snapshot()['status'].get('blackout_latched',True))
        checks.append('UI resume acknowledged by board')
        p.stop_output();wait(lambda:not p.controller.output and not p.controller.busy)
        link=JsonLink('COM10')
        try:
            link.identify();final=link.request('GET_ENGINE');assert not final['enabled']
        finally:link.close()
        checks.append('stop disabled engine and released COM10')
        report={'passed':True,'checks':checks,'before_stop':initial,'after_stop':final,
                'limits':'CC input synthesized; no new physical-keyboard or optical reception verification'}
        Path(sys.argv[1]).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'checks':checks},ensure_ascii=False))
    finally:
        p.close();root.destroy()
