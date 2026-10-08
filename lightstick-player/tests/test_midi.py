# SPDX-License-Identifier: GPL-3.0-only
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import copy
import tempfile
import unittest
import time
from midi_mapping import MappingEngine, default_config, rule, validate, save_config
from midi_io import decode_short
from midi_runtime import MidiController
from midi_link import Sender

class MappingTests(unittest.TestCase):
    def engine(self, *rules):
        config=default_config(); config['rules']=list(rules)
        return MappingEngine(config)
    def cc(self, engine, value, number=74, channel=0):
        return engine.process(dict(type='control_change',channel=channel,control=number,value=value))
    def test_default_real_controls(self):
        e=MappingEngine(default_config())
        self.cc(e,64);self.assertAlmostEqual(e.state['hue'],64/127)
        self.cc(e,0,82);self.assertEqual(e.rgb(),(0,0,0))
        self.cc(e,127,82);self.assertNotEqual(e.rgb(),(0,0,0))
    def test_filters(self):
        e=self.engine(rule(channel=1,number=12))
        self.cc(e,60,12,0);self.cc(e,60,13,1);self.assertEqual(e.state['hue'],0)
        self.cc(e,60,12,1);self.assertAlmostEqual(e.state['hue'],60/127)
    def test_curve_invert_range(self):
        e=self.engine(rule(target='master',invert=True,gamma=2,out_min=.2,out_max=.8))
        self.cc(e,127);self.assertAlmostEqual(e.state['master'],.2)
        self.cc(e,0);self.assertAlmostEqual(e.state['master'],.8)
    def test_relative_formats_and_rgb_accumulation(self):
        for mode,down in [('relative_twos_complement',127),('relative_offset',63),('relative_sign_magnitude',65)]:
            e=self.engine(rule(target='green',mode=mode,step=.1))
            self.cc(e,65 if mode=='relative_offset' else 1)
            self.cc(e,65 if mode=='relative_offset' else 1)
            self.assertAlmostEqual(e.rgb()[1],.2)
            self.cc(e,down);self.assertAlmostEqual(e.rgb()[1],.1)
        e=self.engine(rule(target='master',mode='relative_twos_complement',step=.1,out_min=.2,out_max=.8))
        self.cc(e,1);self.assertAlmostEqual(e.state['master'],.8)
        self.cc(e,120);self.assertAlmostEqual(e.state['master'],.2)
    def test_note_endpoints_and_zero_velocity(self):
        e=MappingEngine(default_config())
        e.process(dict(type='note_on',channel=0,note=72,velocity=127))
        self.assertAlmostEqual(e.state['hue'],24/25)
        before=copy.deepcopy(e.state)
        e.process(dict(type='note_on',channel=0,note=48,velocity=0));self.assertEqual(e.state,before)
    def test_actions_rising_and_release(self):
        e=self.engine(rule(target='pulse',condition='rising'))
        self.assertEqual(len(self.cc(e,127)),1)
        self.assertEqual(self.cc(e,127),[])
        self.assertEqual(self.cc(e,0),[])
        self.assertEqual(len(self.cc(e,127)),1)
        e=self.engine(rule(event='note_on',target='pulse',condition='rising'))
        m=dict(type='note_on',channel=0,note=60,velocity=127)
        self.assertEqual(len(e.process(m)),1);self.assertEqual(e.process(m),[])
        e.process(dict(m,velocity=0));self.assertEqual(len(e.process(m)),1)
    def test_blackout_terminal_resume_hold(self):
        e=self.engine(rule(target='pulse'),rule(target='blackout'),rule(target='master'))
        self.assertEqual([x['cmd'] for x in self.cc(e,127)],['BLACKOUT'])
        self.assertEqual(e.payload()['slots'],[0]*9)
        e.config['rules']=[rule(target='resume')]
        self.assertTrue(self.cc(e,127)[0]['args']['release_blackout']);self.assertFalse(e.blackout)
        e=self.engine(rule(target='hold',condition='rising'),rule(target='master'))
        self.cc(e,127);self.cc(e,0);self.assertTrue(e.hold);self.assertEqual(e.state['master'],1)
    def test_validation_and_roundtrip(self):
        c=default_config();c['rules'][0]['gamma']=float('nan')
        with self.assertRaises(ValueError):validate(c)
        c=default_config();c['rules'][0]['scene']='missing';c['rules'][0]['target']='scene'
        with self.assertRaises(ValueError):validate(c)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'mapping.json';save_config(p,default_config())
            import json
            self.assertEqual(validate(json.loads(p.read_text(encoding='utf-8'))),validate(default_config()))
    def test_learning_does_not_actuate(self):
        c=MidiController(default_config());c.learning=True
        c.receive(dict(type='control_change',channel=0,control=74,value=80))
        self.assertEqual(c.snapshot()['learn_sequence'],1);self.assertEqual(c.engine.state['hue'],0)
    def test_decode_boundaries(self):
        self.assertEqual(decode_short(0xE0)['pitch'],-8192)
        self.assertEqual(decode_short(0x7F7FEF)['pitch'],8191)
        self.assertEqual(decode_short(0x4074B2),dict(type='control_change',channel=2,control=116,value=64))
        self.assertIsNone(decode_short(0xF8))
    def test_sender_blackout_clears_pending_events(self):
        s=Sender(None);s.put({'slots':[1]*9},{'cmd':'TRIGGER','args':{}})
        s.put({'slots':[0]*9},{'cmd':'BLACKOUT','args':{}})
        self.assertEqual([e['cmd'] for e in s.events],['BLACKOUT']);self.assertIsNone(s.latest)
        s.put({'slots':[2]*9});self.assertIsNone(s.latest)
    def test_fine_precision_survives_same_air_bucket(self):
        e=self.engine(rule(target='master'))
        self.cc(e,60);a=e.payload()
        self.cc(e,61);b=e.payload()
        self.assertEqual(a['slots'],b['slots'])
        self.assertNotEqual(a['rgb8'],b['rgb8'])
        self.assertEqual(len(b['rgb8']),27)
    def test_sender_overflow_and_resume_do_not_stop(self):
        s=Sender(None)
        for _ in range(8):self.assertTrue(s.put({'slots':[1]*9},{'cmd':'TRIGGER','args':{}}))
        self.assertFalse(s.put({'slots':[2]*9},{'cmd':'TRIGGER','args':{}}))
        self.assertEqual(s.dropped_events,1);self.assertFalse(s.stop.is_set())
        self.assertTrue(s.put({'slots':[3]*9},{'cmd':'SET_STATE','args':{'release_blackout':True}}))
        self.assertEqual(len(s.events),1)
    def test_sender_snapshot_immutable(self):
        s=Sender(None);state={'slots':[1]*9};s.put(state);state['slots'][0]=8
        self.assertEqual(s.latest['slots'][0],1)
    def test_heartbeat_is_not_starved_and_cadence_is_bounded(self):
        class Link:
            def __init__(self):self.calls=[]
            def request(self,cmd,args=None):
                self.calls.append((cmd,time.monotonic()));time.sleep(.002)
                return {'enabled':True}
        link=Link();s=Sender(link,20);s.thread.start();start=time.monotonic()
        try:
            while time.monotonic()-start<.8:
                s.put({'slots':[int((time.monotonic()-start)*10000)]*9});time.sleep(.001)
        finally:s.stop.set();s.wake.set();s.thread.join(1)
        self.assertIsNone(s.error)
        self.assertIn('GET_ENGINE',[x[0] for x in link.calls])
        times=[x[1] for x in link.calls if x[0]=='SET_STATE']
        self.assertGreater(len(times),10);self.assertLessEqual(len(times),18)
        self.assertTrue(all(b-a>=.045 for a,b in zip(times,times[1:])))

if __name__=='__main__':unittest.main()
