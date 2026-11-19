import unittest
from unittest.mock import patch
from scripts import coverage_standby as s


class Queue(unittest.TestCase):
    def setUp(self):
        self.q = dict(status='queued', runner_name='', runner_id=0, id=2)
        self.active = dict(self.q, status='in_progress', runner_name='cs-jed-nc-n1-pr-v1-abc-runner-def', runner_id=123)
        self.env = dict(GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='42', GROUP='0', HEAD='a'*40, SOURCE='fork/matrixone', PR='32922')

    def test_deadline(self):
        self.assertEqual(s.decide(self.q, 50, None), (None, 50))
        self.assertEqual(s.decide(self.q, 649, 50), (None, 50))
        self.assertEqual(s.decide(self.q, 650, 50), ('idc', 50))
        self.assertEqual(s.decide(self.active, 670, 50), ('cs', 50))
        self.assertEqual(s.decide(dict(self.q, runner_id=2), 670, 50), (None, None))
        self.assertEqual(s.decide(dict(self.q, status='waiting'), 670, 50), (None, None))
        with self.assertRaises(ValueError):
            s.decide(dict(self.q, status='completed'), 670, 50)

    def scenario(self, observations, cancellation, count, selected):
        import tempfile, os, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**self.env, 'RUNNER_TEMP': tmp}), \
             patch.object(s,'parent_meta',return_value={'head_sha':'a'*40,'head_repository':{'full_name':'fork/matrixone'}}), \
             patch.object(s,'one',return_value={'object':{'sha':'b'*40}}), \
             patch.object(s,'dispatch',side_effect=[111,222]) as launch, \
             patch.object(s,'observe',side_effect=observations), \
             patch.object(s,'child_meta',return_value={'status':'completed','conclusion':'cancelled'}), \
             patch.object(s,'api') as api, patch.object(s.time,'sleep'), patch.object(s,'output'):
            s.choose()
            self.assertEqual(launch.call_count, count)
            self.assertEqual(api.call_count, int(cancellation))
            if cancellation:
                api.assert_called_once_with('/actions/runs/111/cancel', {})
            grant=json.loads(Path(tmp,'grant.json').read_text())
            self.assertEqual((grant['pool'],grant['child']),selected)
            self.assertEqual(grant['ticket']['key'],s.digest(grant['nonce']))

    def test_only_the_still_queued_child_is_withdrawn(self):
        self.scenario([(self.q,1),(self.q,601),(self.q,602)],True,2,('idc',222))

    def test_assigned_during_final_read_is_never_cancelled(self):
        self.scenario([(self.q,1),(self.q,601),(self.active,602)],False,1,('cs',111))

    def test_immediate_assignment(self):
        self.scenario([(self.active,2)],False,1,('cs',111))
