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
    def test_moving_ref_reconciles_only_verified_ungranted_child(self):
        for pool in ('cs', 'idc'):
            t = dict(parent=1, attempt=1, group=0, head='a'*40)
            rev1, rev2 = 'b'*40, 'c'*40
            events, dispatched = [], []
            def post(path, data):
                if path.endswith('/cancel'):
                    events.append('cancel')
                    self.assertEqual(path, '/actions/runs/101/cancel')
                    return {}, 0
                dispatched.append(json.loads(data['inputs']['ticket']))
                events.append('dispatch')
                return {'workflow_run_id': 100 + len(dispatched)}, 0
            def metadata(r):
                stopped = 'cancel' in events and r == 101
                if stopped:
                    events.append('confirmed')
                return dict(id=r, repository={'full_name':s.REPO},run_attempt=1,
                    path='.github/workflows/'+s.WORKER,event='workflow_dispatch',
                    head_branch=s.WORKER_REF,head_sha=rev2,
                    display_title=s.title(dispatched[r-101],pool),
                    status='completed' if stopped else 'queued')
            with patch.object(s,'parent_meta'), patch.object(s,'api',side_effect=post), \
                 patch.object(s,'one',side_effect=[{'object':{'sha':x}} for x in (rev1,rev2)]), \
                 patch.object(s,'run',side_effect=metadata):
                r, rev, nonce = s.reserve(t, pool)
            self.assertEqual(events, ['dispatch','cancel','confirmed','dispatch'])
            self.assertEqual((r,rev,s.digest(nonce)),(102,rev2,dispatched[1]['key']))
            self.assertNotEqual(dispatched[0]['key'],dispatched[1]['key'])

    def test_revision_retry_fails_closed(self):
        t = dict(parent=1, attempt=1, group=0, head='a'*40)
        for error, count in [(ValueError('identity mismatch'),0),
                             (s.DispatchRevisionChanged(101,'c'*40),1)]:
            with patch.object(s,'one',return_value={'object':{'sha':'b'*40}}), \
                 patch.object(s,'parent_meta'), patch.object(s,'dispatch',side_effect=error) as send, \
                 patch.object(s,'child_meta',return_value={'status':'queued'}), \
                 patch.object(s,'api') as post, patch.object(s.time,'sleep'):
                with self.assertRaises(ValueError):
                    s.reserve(t,'idc')
                self.assertEqual((send.call_count,post.call_count),(1,count))

    def test_idc_diagnostics_preserve_assertion_and_internal_timeout(self):
        import subprocess, tempfile, textwrap
        from pathlib import Path
        wf = (Path(__file__).resolve().parents[1]/'.github/workflows/coverage-worker-idc.yaml').read_text()
        diag = wf.split('    - name: Collect bounded Shanghai failure evidence\n')[1]
        before, after = diag.split('    - name: Record actual packages',1)
        self.assertEqual(before.count("steps.coverage_ut.outcome == 'failure'"),2)
        self.assertIn('exit "${test_status}"', wf.split('    - name: Collect bounded')[0])
        script = textwrap.dedent(before.split('      run: |\n')[1].split('    - name: Upload')[0])
        for reason in ('assertion', 'timeout'):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                if reason == 'assertion':
                    rows = [dict(Action='output',Package='p',Test='t',Output='expected 3, got 2'),
                            dict(Action='fail',Package='p',Test='t')]
                    (root/'ut-coverage-report.json').write_text('\n'.join(map(json.dumps,rows)))
                else:
                    (root/'ut-coverage-timeout.txt').write_text('deadline reached: coverage_ut')
                output = root/'outputs'
                subprocess.run(['bash','-c',script],check=True,capture_output=True,
                    env={**os.environ,'RUNNER_TEMP':d,'GITHUB_OUTPUT':str(output)})
                dst = Path(output.read_text().strip().split('=',1)[1])
                if reason == 'assertion':
                    self.assertIn('expected 3, got 2',(dst/'failed-test-output.txt').read_text())
                else:
                    self.assertIn('deadline reached',(dst/'ut-coverage-timeout.txt').read_text())

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


class Progress(unittest.TestCase):
    def test_live_worker_link_and_known_phases(self):
        for pool, runner, step, site in (
                ("cs", "cs-jed-nc-n1-pr-v1-abc-runner-def", "Run coverage unit tests", "JED"),
                ("idc", "amd64-mo-shanghai-ut-safe-abc-runner-def",
                 "Run the reviewed Coverage UT group, cold build allowed", "Shanghai IDC")):
            for status, phase in (("pending", "runner assigned; preparing Coverage UT"),
                                  ("in_progress", "Coverage UT running (setup and tests)"),
                                  ("completed", "UT step finished; collecting results")):
                target = dict(id=312, runner_id=7, runner_name=runner, status="in_progress",
                              steps=[dict(name=step, status=status)])
                with self.subTest(pool=pool, status=status), patch.object(s, "observe", return_value=(target, 1)) as read:
                    self.assertEqual(s.worker_progress(123, 1, pool),
                                     (f"{site}: {phase}", f"https://github.com/{s.REPO}/actions/runs/123/job/312"))
                    read.assert_called_once_with(123, {"group": 1}, pool)

    def test_progress_is_best_effort_and_child_result_is_authoritative(self):
        env = dict(CHILD="123", GROUP="1", POOL="cs")
        for conclusion in ("success", "failure"):
            with self.subTest(conclusion=conclusion), patch.dict(os.environ, env), \
                 patch.object(s, "run", side_effect=[dict(status="in_progress"),
                                                    dict(status="completed", run_attempt=1, conclusion=conclusion)]), \
                 patch.object(s, "worker_progress", side_effect=OSError("do not print API details")), \
                 patch.object(s.time, "sleep"), patch("builtins.print") as out:
                if conclusion == "success":
                    s.wait()
                else:
                    with self.assertRaisesRegex(ValueError, "selected coverage group did not pass"):
                        s.wait()
                self.assertIn("worker details temporarily unavailable", str(out.call_args_list))
                self.assertNotIn("do not print API details", str(out.call_args_list))


class ShortWorkerTitle(unittest.TestCase):
    def setUp(self):
        self.ticket = dict(pr=33174, parent=42, attempt=1, group=0, head="a"*40,
                           key="c"*64, title_version=2)
        self.meta = dict(id=51, repository={"full_name": s.REPO}, run_attempt=1,
                         path=".github/workflows/"+s.WORKER, event="workflow_dispatch",
                         head_branch=s.WORKER_REF, head_sha="b"*40,
                         display_title="GROUP0")
        self.marker = dict(run_id=51, run_attempt=1, status="completed", conclusion="skipped",
                           name=s.title(self.ticket, "cs")+s.IDENTITY_SUFFIX)

    def test_exact_identity_still_required(self):
        self.assertEqual(s.short_title(self.ticket), "GROUP0")
        self.assertEqual(s.short_title(dict(self.ticket, group=1)), "GROUP1")
        for marker, match in ((self.marker, True),
                              (dict(self.marker, run_id=52), False),
                              (dict(self.marker, conclusion="success"), False),
                              (dict(self.marker, name=s.title(dict(self.ticket, group=1), "cs")+s.IDENTITY_SUFFIX), False),
                              (dict(self.marker, name=self.marker["name"].replace("c"*64, "e"*64)), False)):
            with self.subTest(marker=marker), patch.object(s, "run", return_value=self.meta), \
                 patch.object(s, "one", return_value=dict(total_count=1, jobs=[marker])):
                if match:
                    self.assertEqual(s.child_meta(51, self.ticket, "cs", "b"*40), self.meta)
                else:
                    with self.assertRaisesRegex(ValueError, "identity"):
                        s.child_meta(51, self.ticket, "cs", "b"*40)
        legacy = dict(self.meta, display_title=s.title(self.ticket, "cs"))
        with patch.object(s, "run", return_value=legacy), patch.object(s, "one") as lookup:
            self.assertEqual(s.child_meta(51, self.ticket, "cs", "b"*40), legacy)
            lookup.assert_not_called()

    def test_dispatch_reconciles_two_identical_human_titles(self):
        other = dict(self.meta, id=52)
        wrong_marker = dict(self.marker, run_id=52, name=s.title(dict(self.ticket, parent=43), "cs")+s.IDENTITY_SUFFIX)
        def read(path):
            if "/actions/workflows/" in path:
                return {"workflow_runs": [other, self.meta]}
            return {"total_count": 1, "jobs": [wrong_marker if "/52/" in path else self.marker]}
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({}, 0)) as post, \
             patch.object(s, "one", side_effect=read), patch.object(s, "run", return_value=self.meta):
            self.assertEqual(s.dispatch(self.ticket, "cs", "b"*40), 51)
            post.assert_called_once()
