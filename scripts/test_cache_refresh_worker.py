import unittest
from unittest.mock import patch

from scripts import cache_refresh_worker as w


class Worker(unittest.TestCase):
    def setUp(self):
        self.runner = 'cs-mty-nc-n0-pr-v1-abc-runner-def'
        self.head, self.rev, self.pin = ('a'*40, 'b'*40, 'c'*40)
        uid = '00000000-0000-0000-0000-000000000001'
        profile = dict(site='mty', node='mo-standard-0', node_uid=uid, pv_uid=uid,
                       pvc_uid=uid, runtime='d'*64, native='e'*64, modules='f'*64,
                       recipe=w.demand.RECIPE)
        self.h = dict(schema=1, head=self.head, profile=profile, runtime_missing=True, native_missing=True)
        self.a = dict(shared_hits=0, go_compiler_invocations=15, native_hit=False)
        self.ticket = dict(parent=1, attempt=1, group=0, head=self.head, revision=self.rev,
                           key=w.standby.digest('nonce'), source='fork/matrixone')
        repo = {'full_name': w.standby.REPO}
        self.child = dict(id=2, repository=repo, head_repository=repo, event='workflow_dispatch',
                          head_branch='main', path='.github/workflows/coverage-standby-worker.yaml',
                          run_attempt=1, status='completed', conclusion='success', head_sha=self.rev,
                          display_title=w.standby.title(self.ticket, 'cs'), referenced_workflows=[
                              dict(path='matrixorigin/CI/.github/workflows/coverage-worker-cs.yaml@'+self.pin,
                                   sha=self.pin)])

    def test_screen_realistic_granted_child(self):
        grant = dict(ticket=self.ticket, child=2, nonce='nonce', pool='cs')
        parent = dict(id=1, run_attempt=1, repository=self.child['repository'],
                      path=w.standby.ENTRY, event='pull_request_target', head_sha=self.head,
                      head_repository={'full_name': self.ticket['source']})
        job = dict(conclusion='success', runner_id=3, runner_name=self.runner)
        with patch.object(w.standby, 'run', return_value=self.child), \
             patch.object(w.standby, 'artifact', return_value=grant), \
             patch.object(w.standby, 'one', side_effect=[parent, {'artifacts':[{}]}]), \
             patch.object(w.standby, 'observe', return_value=(job, 0)), \
             patch.object(w, 'archive', return_value=(self.h, self.a)):
            self.assertEqual(w.evidence(2, self.pin)[0], 'inspect')

    def test_cannot_submit_trial_or_other_revision(self):
        for change in [dict(head_branch='weilu-runner-test'), dict(run_attempt=2),
                       dict(referenced_workflows=[]), dict(event='push')]:
            with self.subTest(change=change), patch.object(w.standby, 'run',
                    return_value={**self.child, **change}), self.assertRaises(ValueError):
                w.evidence(2, self.pin)

    def test_idc_ignored(self):
        self.child['display_title'] = w.standby.title(self.ticket, 'idc')
        with patch.object(w.standby, 'run', return_value=self.child):
            self.assertEqual(w.evidence(2, self.pin), ('not-cs', None))

    def test_hot_read_never_enqueues_writer(self):
        result = w.route(({**self.h, 'runtime_missing':False, 'native_missing':False},
                          dict(shared_hits=180, go_compiler_invocations=0, native_hit=True)),
                         self.head, self.runner)
        self.assertEqual(result, 'no-confirmed-miss')

    def test_wrong_head_node_or_counter_rejected(self):
        for h, a, runner in [({**self.h,'head':'b'*40},self.a,self.runner),
                             (self.h,self.a,self.runner.replace('n0','n1')),
                             (self.h,{**self.a,'shared_hits':False},self.runner)]:
            with self.assertRaises(ValueError):
                w.route((h,a),self.head,runner)
