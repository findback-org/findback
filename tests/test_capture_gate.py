"""Capture must begin before any actor stage; partial/replayed cues cannot post."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import tempfile
import unittest

SPEC=spec_from_file_location('capture_gate',Path(__file__).parents[1]/'scripts/run_capture_gate.py')
MODULE=module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CaptureGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.state=Path(self.temp.name)
        self.calls=[];self.journal={'stages':{}}
        owner=self
        class Take:
            def __init__(self,state,*,pace):self.journal=owner.journal
            def __getattr__(self,name):
                def stage():
                    owner.calls.append(name)
                    value={'completed_at':'original-'+name}
                    self.journal['stages'][name]=value
                    return value
                return stage
            def close(self):pass
            def stop(self):pass
        self.factory=Take

    def tearDown(self):self.temp.cleanup()

    def cue(self,stage,nonce):
        (self.state/'capture-cue.txt').write_text(json.dumps({'stage':stage,'nonce':nonce}))

    def gate(self):return MODULE.CaptureGate(self.state,delay=0,take_factory=self.factory)

    def status(self):return json.loads((self.state/'capture-status.json').read_text())

    def test_startup_ignores_existing_cue_and_requires_fresh_nonce(self):
        self.cue('context','old');gate=self.gate()
        self.assertFalse(gate.tick());self.assertEqual([],self.calls)
        self.cue('context','old');self.assertFalse(gate.tick());self.assertEqual([],self.calls)
        self.cue('context','new');self.assertTrue(gate.tick());self.assertEqual(['context'],self.calls)

    def test_partial_json_is_not_consumed_and_worker_remains_usable(self):
        gate=self.gate();(self.state/'capture-cue.txt').write_text('{"stage":"context",')
        self.assertFalse(gate.tick());self.assertEqual('WAITING_FOR_COMPLETE_CUE',self.status()['status'])
        self.assertIsNone(gate.last_signature);self.assertEqual([],self.calls)
        self.cue('context','complete');self.assertTrue(gate.tick());self.assertEqual(['context'],self.calls)

    def test_completed_stage_is_noop_and_cannot_report_new_arrival(self):
        gate=self.gate();self.cue('context','1');gate.tick();original=self.journal['stages']['context'].copy()
        self.cue('context','2');gate.tick()
        self.assertEqual(['context'],self.calls);self.assertEqual('NOOP',self.status()['status'])
        self.assertEqual(original,self.status()['original_result']);self.assertEqual(0,self.status()['posts_sent'])

    def test_out_of_order_cue_does_not_implicitly_run_an_earlier_stage(self):
        gate=self.gate();self.cue('claim','wrong');gate.tick()
        self.assertEqual([],self.calls);self.assertEqual('REFUSED',self.status()['status'])
        self.assertEqual('context',self.status()['expected_stage'])
        self.cue('context','first');gate.tick();self.cue('found','second');gate.tick()
        self.assertEqual(['context','found'],self.calls)

    def test_constructor_failure_does_not_kill_gate(self):
        gate=self.gate()
        gate.take_factory=lambda *_args,**_kwargs: (_ for _ in ()).throw(RuntimeError('setup failed'))
        self.cue('context','broken');self.assertFalse(gate.tick());self.assertEqual('FAILED',self.status()['status'])
        gate.take_factory=self.factory
        self.cue('context','retry');self.assertTrue(gate.tick());self.assertEqual(['context'],self.calls)

    def test_cue_written_while_stage_runs_requires_fresh_capture_cue(self):
        owner=self
        class During(self.factory):
            def context(self):
                owner.calls.append('context');value={'completed_at':'original-context'}
                self.journal['stages']['context']=value
                owner.cue('found','premature')
                return value
        gate=MODULE.CaptureGate(self.state,delay=0,take_factory=During)
        self.cue('context','first');gate.tick()
        self.assertEqual('found',self.status()['queued_cue_refused']['stage'])
        self.assertFalse(gate.tick());self.assertEqual(['context'],self.calls)
        self.cue('found','fresh-recording');gate.tick();self.assertEqual(['context','found'],self.calls)


if __name__=='__main__':unittest.main()
