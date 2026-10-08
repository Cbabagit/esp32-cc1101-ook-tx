# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the real Tk UI in source and frozen builds without transmitting RF."""
import json
import os
import tempfile
from pathlib import Path

def run(output):
    result={}
    try:
        with tempfile.TemporaryDirectory() as temporary:
            os.environ['LIGHTSTICK_CONFIG_DIR']=temporary
            import tkinter as tk
            from gui import LightstickPlayerApp
            root=tk.Tk();root.withdraw()
            app=LightstickPlayerApp(root);panel=app.midi_panel
            try:
                assert len(app.tabs.tabs())==2
                panel.fade.set('250');panel.settle.set('3');panel.apply_options()
                assert panel.config['fade_ms']==250 and panel.config['settle_frames']==3
                panel.add();assert len(panel.config['rules'])==6
                panel.form['name'].set('自定义推子')
                panel.form['number'].set('23')
                panel.form['target'].set('主亮度')
                panel.enabled.set(True);panel.apply()
                assert panel.config['rules'][5]['number']==23
                panel.duplicate();assert len(panel.config['rules'])==7
                panel.remove();assert len(panel.config['rules'])==6
                panel.save();assert panel.path.exists()
                saved=json.loads(panel.path.read_text(encoding='utf-8'))
                assert saved['rules'][5]['name']=='自定义推子'
                panel.controller.learning=True;panel.learn_index=5
                panel.controller.receive(dict(type='control_change',channel=3,control=24,value=70))
                panel.poll();assert panel.config['rules'][5]['number']==24
                assert panel.config['rules'][5]['channel']==3
                panel.controller.receive(dict(type='control_change',channel=3,control=24,value=64))
                assert abs(panel.controller.snapshot()['state']['master']-64/127)<1e-8
                panel.action('blackout');assert panel.controller.snapshot()['blackout']
                panel.action('resume');assert not panel.controller.snapshot()['blackout']
                root.update_idletasks()
                result={'passed':True,'checks':['two tabs','smooth output settings','rule add/edit/duplicate/delete','save JSON','learn channel/CC','mapped preview','blackout/resume'], 'tk':tk.TkVersion}
            finally:panel.close();root.destroy()
    except Exception:
        import traceback
        result={'passed':False,'error':traceback.format_exc()}
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1

if __name__=='__main__':
    import sys
    raise SystemExit(run(sys.argv[1]))
