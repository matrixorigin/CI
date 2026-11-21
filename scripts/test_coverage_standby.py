import unittest
import json
import os
from unittest.mock import patch
from scripts import coverage_standby as s


class ParentAdmission(unittest.TestCase):
    def setUp(self):
        self.sha = '3b95c0878f88b3da58bf3e65c369b7ef4d13104e'
        self.src = 'Ariznawlll/matrixone'
        self.t = dict(parent=42, attempt=1, group=0, pr=32922, source=self.src, head=self.sha,
                      key=s.digest('test-nonce'), revision='b'*40)
        self.p = dict(id=42, run_attempt=1, repository={'full_name':s.REPO}, path=s.ENTRY,
                      event='pull_request_target', head_repository={'full_name':self.src},
                      head_sha=self.sha, status='in_progress', conclusion=None)

    def test_rerun_parent_identity_is_attempt_bound(self):
        rerun = dict(self.t,attempt=2)
        s.parent_identity(rerun,dict(self.p,run_attempt=2))
        with self.assertRaisesRegex(ValueError,'identity mismatch: run_attempt'):
            s.parent_identity(rerun,self.p)

    def test_only_active_parent_returns(self):
        with patch.object(s,'run',side_effect=[dict(self.p,status=v) for v in
                          ('queued','waiting','in_progress')]) as read, patch.object(s.time,'sleep') as sleep:
            self.assertEqual(s.parent_meta(self.t)['id'],42)
            self.assertEqual((read.call_count,sleep.call_count),(3,2))

    def test_queued_parent_revalidated_each_time(self):
        for field,bad in [('id',43),('run_attempt',2),('repository',{'full_name':'fork/other'}),
                          ('path','wrong-workflow'),('event','workflow_dispatch'),
                          ('head_repository',None),('head_sha','c'*40)]:
            with self.subTest(field=field), patch.object(s,'run',side_effect=[
                    dict(self.p,status='queued'),{**self.p,field:bad}]), patch.object(s.time,'sleep') as sleep:
                with self.assertRaisesRegex(ValueError,'identity mismatch: '+field):
                    s.parent_meta(self.t)
                self.assertEqual(sleep.call_count,1)

    def test_terminal_unknown_and_incomplete_states_fail_closed(self):
        for p in (dict(self.p,status='completed'),dict(self.p,conclusion='cancelled'),
                  dict(self.p,status='unexpected'),{k:v for k,v in self.p.items() if k!='conclusion'}):
            with self.subTest(p=p), patch.object(s,'run',return_value=p), patch.object(s.time,'sleep') as sleep:
                with self.assertRaisesRegex(ValueError,'not active'):
                    s.parent_meta(self.t)
                sleep.assert_not_called()

    def test_queued_is_bounded_and_never_grants(self):
        with patch.object(s,'run',return_value=dict(self.p,status='queued')) as read, \
             patch.object(s.time,'sleep') as sleep, patch.object(s,'artifact') as artifact:
            with self.assertRaisesRegex(ValueError,'still not active'):
                s.parent_meta(self.t)
            self.assertEqual((read.call_count,sleep.call_count),(s.PARENT_POLLS,s.PARENT_POLLS-1))
            artifact.assert_not_called()

    def test_real_grant_path_and_terminal_change_before_checkout(self):
        grant=dict(child=51,ticket=self.t,pool='cs',nonce='test-nonce')
        proof=dict(allowed='true',verified_membership_http_status=204,pr_number='32922',
                   author='dev',head_repository=self.src,head_sha=self.sha)
        pr=dict(number=32922,user={'login':'dev'},head={'repo':{'full_name':self.src,'archived':False},
                'sha':self.sha},state='open',draft=False,base={'repo':{'full_name':s.REPO},'ref':'main'},
                labels=[{'name':'kind/test-ci'}])
        env=dict(TICKET=json.dumps(self.t),GROUP='0',HEAD=self.sha,SOURCE=self.src,POOL='cs',
                 GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='51')
        for cancel in (False,True):
            reads=[dict(self.p,status='queued'),self.p,self.p,
                   dict(self.p,status='completed',conclusion='cancelled') if cancel else self.p]
            with self.subTest(cancel=cancel), patch.dict(os.environ,env), patch.object(s,'run',side_effect=reads), \
                 patch.object(s,'child_meta'), patch.object(s.time,'sleep'), patch.object(s,'one',return_value=pr), \
                 patch.object(s,'artifact',side_effect=[grant,proof]):
                if cancel:
                    with self.assertRaisesRegex(ValueError,'cancelled'):
                        s.admit()
                else:
                    s.admit()


class Queue(unittest.TestCase):
    def setUp(self):
        self.q = dict(status='queued', runner_name='', runner_id=0, id=2)
        self.active = dict(self.q, status='in_progress', runner_name='cs-jed-nc-n1-pr-v1-abc-runner-def', runner_id=123)
        self.env = dict(GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='42', GROUP='0', HEAD='a'*40, SOURCE='fork/matrixone', PR='32922')

    def test_deadline(self):
        self.assertEqual(s.decide(self.q, 50, None), (None, 50))
        self.assertEqual(s.decide(self.q, 3649, 50), (None, 50))
        self.assertEqual(s.decide(self.q, 3650, 50), ('idc', 50))
        self.assertEqual(s.decide(self.active, 3670, 50), ('cs', 50))
        self.assertEqual(s.decide(dict(self.q, runner_id=2), 3670, 50), (None, 50))
        self.assertEqual(s.decide(dict(self.q, status='waiting'), 3670, 50), (None, 50))
        with self.assertRaises(ValueError):
            s.decide(dict(self.q, status='completed'), 3670, 50)

    def scenario(self, observations, cancellation, count, selected, *, attempt='1', revisions=None):
        import tempfile, os, json
        from pathlib import Path
        if revisions is None:
            revisions = ['b'*40] * (1 + int(cancellation))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**self.env, 'GITHUB_RUN_ATTEMPT': attempt, 'RUNNER_TEMP': tmp}), \
             patch.object(s,'parent_meta',return_value={'head_sha':'a'*40,'head_repository':{'full_name':'fork/matrixone'}}), \
             patch.object(s,'one',side_effect=[{'object':{'sha':sha}} for sha in revisions]), \
             patch.object(s,'require_previous_grants_terminal') as previous, \
             patch.object(s,'dispatch',side_effect=[111,222]) as launch, \
             patch.object(s,'observe',side_effect=observations), \
             patch.object(s,'child_meta',return_value={'status':'completed','conclusion':'cancelled'}), \
             patch.object(s,'api') as api, patch.object(s.time,'sleep'), patch.object(s,'output'):
            s.choose()
            self.assertEqual(previous.call_count, int(int(attempt) > 1))
            self.assertEqual(launch.call_count, count)
            self.assertEqual(api.call_count, int(cancellation))
            if cancellation:
                api.assert_called_once_with('/actions/runs/111/cancel', {})
            grant=json.loads(Path(tmp,'grant.json').read_text())
            self.assertEqual((grant['pool'],grant['child']),selected)
            self.assertEqual(grant['ticket']['key'],s.digest(grant['nonce']))
            self.assertEqual(grant['ticket']['attempt'],int(attempt))
            self.assertEqual(grant['ticket']['revision'],revisions[-1])
            self.assertEqual([call.args[2] for call in launch.call_args_list],revisions)

    def test_only_the_still_queued_child_is_withdrawn(self):
        self.scenario([(self.q,1),(self.q,3601),(self.q,3602)],True,2,('idc',222))

    def test_rerun_uses_attempt_bound_grant_and_fresh_shanghai_revision(self):
        self.scenario([(self.q,1),(self.q,3601),(self.q,3602)],True,2,('idc',222),
                      attempt='2', revisions=['b'*40,'c'*40])

    def test_assigned_during_final_read_is_never_cancelled(self):
        self.scenario([(self.q,1),(self.q,3601),(self.active,3602)],False,1,('cs',111))

    def test_queued_assignment_metadata_can_settle_before_grant(self):
        transitional = dict(self.q,runner_id=123,runner_name=self.active['runner_name'])
        self.scenario([(self.q,1),(self.q,3601),(transitional,3602),(self.active,3603)],
                      False,1,('cs',111))

    def test_ambiguous_handoff_exhausts_bound_without_cancelling_or_granting(self):
        import tempfile
        transitional = dict(self.q,runner_id=123,runner_name=self.active['runner_name'])
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{**self.env,'RUNNER_TEMP':tmp}), \
             patch.object(s,'HANDOFF_POLLS',3), patch.object(s,'parent_meta'), \
             patch.object(s,'one',return_value={'object':{'sha':'b'*40}}), \
             patch.object(s,'dispatch',return_value=111), \
             patch.object(s,'observe',side_effect=[(self.q,1),(self.q,3601)]+[(transitional,3602)]*3), \
             patch.object(s,'child_meta'), patch.object(s,'api') as api, patch.object(s.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'did not settle'):
                s.choose()
            api.assert_not_called()

    def test_immediate_assignment(self):
        self.scenario([(self.active,2)],False,1,('cs',111))

    def test_empty_dispatch_response_reconciles_on_same_worker_ref_without_second_post(self):
        t = dict(parent=1, attempt=1, group=1, head='a'*40, key='b'*64)
        with patch.object(s, 'WORKER_REF', 'staging'), patch.object(s, 'parent_meta'), \
             patch.object(s, 'child_meta'), patch.object(s, 'api', return_value=({},0)) as post, \
             patch.object(s, 'one', return_value={'workflow_runs':[{'id':123,'display_title':s.title(t,'cs')}]}) as get:
            self.assertEqual(s.dispatch(t,'cs','c'*40),123)
            self.assertEqual(post.call_count,1)
            self.assertEqual(post.call_args.args[1]['ref'],'staging')
            self.assertIn('branch=staging',get.call_args.args[0])

    def test_proof_is_specific_to_retry_group_and_attempt(self):
        t = dict(parent=42, group=1, attempt=1)
        self.assertEqual(s.proof_name(t), "real-pr-snapshot-42-attempt-1")
        self.assertEqual(s.proof_name(dict(t,attempt=2)), "real-pr-retry-42-attempt-2-group-1")

    def test_live_old_grant_blocks_only_its_group(self):
        t = dict(parent=42,group=0,attempt=2,head='a'*40)
        old = dict(display_title=s.title(dict(t,attempt=1,key='a'*64), 'cs'),
                   path='.github/workflows/'+s.WORKER, event='workflow_dispatch',
                   repository={'full_name':s.REPO},head_branch=s.WORKER_REF,status='in_progress')
        with patch.object(s,'one',return_value={'artifacts':[{'name':'coverage-admit-81'}]}), \
             patch.object(s,'run',return_value=old):
            with self.assertRaisesRegex(ValueError,'still active'):
                s.require_previous_grants_terminal(t)
            s.require_previous_grants_terminal(dict(t,group=1))
            old['status'] = 'completed'
            s.require_previous_grants_terminal(t)

    def test_failed_only_job_boundary(self):
        from pathlib import Path
        wf = (Path(__file__).resolve().parents[1]/'.github/workflows/coverage-queue-handoff.yaml').read_text()
        selector, collector = wf.split('  collect:\n')
        self.assertLess(selector.index('coverage_standby.py choose'), selector.index('coverage_standby.py wait'))
        self.assertIn('real-pr-retry-', selector)
        self.assertNotIn('coverage_standby.py wait', collector)
        self.assertIn('actions: read', collector)
        self.assertGreater(85*60, s.WAIT)
