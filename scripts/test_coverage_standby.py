import unittest
import json
import os
import tempfile
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from scripts import coverage_standby as s
from scripts.coverage_pr_entry import RELEASE_BASE


class ParentAdmission(unittest.TestCase):
    def setUp(self):
        self.sha = '3b95c0878f88b3da58bf3e65c369b7ef4d13104e'
        self.src = 'Ariznawlll/matrixone'
        self.t = dict(parent=42, attempt=1, group=0, pr=32922, source=self.src, head=self.sha,
                      key=s.digest('test-nonce'), revision='b'*40)
        self.p = dict(id=42, run_attempt=1, repository={'full_name':s.REPO}, path=s.ENTRY,
                      event='pull_request_target', head_repository={'full_name':self.src},
                      head_sha=self.sha, status='in_progress', conclusion=None)
        self.grant = dict(child=51, ticket=self.t, pool='cs', nonce='test-nonce')
        self.worker = dict(id=51, repository={'full_name':s.REPO}, run_attempt=1,
                           path='.github/workflows/'+s.WORKER, event='workflow_dispatch',
                           head_branch=s.WORKER_REF, head_sha=self.t['revision'],
                           display_title=s.title(self.t, 'cs'), status='in_progress')
        self.selector_env = dict(GITHUB_RUN_ID='42', GITHUB_RUN_ATTEMPT='1', GROUP='0', PR='32922',
                                 HEAD=self.sha, SOURCE=self.src, CHILD='51')

    def test_rerun_parent_identity_is_attempt_bound(self):
        rerun = dict(self.t,attempt=2)
        s.parent_identity(rerun,dict(self.p,run_attempt=2))
        with self.assertRaisesRegex(ValueError,'identity mismatch: run_attempt'):
            s.parent_identity(rerun,self.p)

    def test_only_active_parent_returns(self):
        with patch.object(s,'run',side_effect=[dict(self.p,status=v) for v in
                          ('queued','waiting','in_progress')]) as read, \
             patch.object(s,'selector_active',return_value=False), patch.object(s.time,'sleep') as sleep:
            self.assertEqual(s.parent_meta(self.t)['id'],42)
            self.assertEqual((read.call_count,sleep.call_count),(3,2))

    def test_queued_parent_revalidated_each_time(self):
        for field,bad in [('id',43),('run_attempt',2),('repository',{'full_name':'fork/other'}),
                          ('path','wrong-workflow'),('event','workflow_dispatch'),
                          ('head_repository',None),('head_sha','c'*40)]:
            with self.subTest(field=field), patch.object(s,'run',side_effect=[
                    dict(self.p,status='queued'),{**self.p,field:bad}]), \
                 patch.object(s,'selector_active',return_value=False), patch.object(s.time,'sleep') as sleep:
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
             patch.object(s,'selector_active',return_value=False), \
             patch.object(s.time,'sleep') as sleep, patch.object(s,'artifact') as artifact:
            with self.assertRaisesRegex(ValueError,'still not active'):
                s.parent_meta(self.t)
            self.assertEqual((read.call_count,sleep.call_count),(s.PARENT_POLLS,s.PARENT_POLLS-1))
            artifact.assert_not_called()

    def selector(self, group=0, **changes):
        return {
            'id': 123,
            'run_id': 42,
            'run_attempt': 1,
            'name': f'Matrixone UT Coverage execution / Route Coverage group {group} / select',
            'status': 'in_progress',
            'conclusion': None,
            'steps': [
                {'name': 'Release exactly one worker', 'status': 'pending', 'conclusion': None},
                {'name': 'Wait for selected group, including its UT result', 'status': 'pending', 'conclusion': None},
                {'name': 'Cancel the selected worker if the selector stops', 'status': 'pending', 'conclusion': None},
            ],
            **changes,
        }

    def test_queued_run_admits_only_its_active_selector(self):
        queued = dict(self.p, status='queued')
        with patch.object(s,'run',return_value=queued) as read, \
             patch.object(s,'one',return_value={'jobs':[self.selector()]}) as jobs, \
             patch.object(s.time,'sleep') as sleep:
            self.assertEqual(s.parent_meta(self.t),queued)
            self.assertEqual(read.call_count,2)
            jobs.assert_called_once_with('/actions/runs/42/attempts/1/jobs?per_page=100&page=1')
            sleep.assert_not_called()

    def test_selector_lookup_checks_group_attempt_and_pages(self):
        others = [self.selector(group=1,id=i) for i in range(100)]
        with patch.object(s,'one',side_effect=[{'jobs':others}, {'jobs':[self.selector()]}]) as jobs:
            self.assertTrue(s.selector_active(self.t))
            self.assertEqual(jobs.call_count,2)
        stopped_waits = [
            self.selector(steps=[
                {'name': 'Release exactly one worker', 'conclusion': 'success'},
                {'name': 'Wait for selected group, including its UT result', 'conclusion': conclusion},
            ])
            for conclusion in ('failure', 'cancelled', 'timed_out', 'skipped')
        ]
        for job in (self.selector(group=1), self.selector(status='completed', conclusion='cancelled'),
                    self.selector(status='queued'), self.selector(steps=[]),
                    *stopped_waits,
                    self.selector(steps=[{'name': 'Release exactly one worker', 'conclusion': 'success'}]),
                    self.selector(steps=[{'name': 'Release exactly one worker', 'conclusion': 'success'},
                                         {'name': 'Wait for selected group, including its UT result', 'conclusion': None},
                                         {'name': 'Wait for selected group, including its UT result', 'conclusion': 'skipped'}]),
                    self.selector(steps=[{'name': 'Release exactly one worker', 'conclusion': 'skipped'}]),
                    self.selector(steps=[{'name': 'Release exactly one worker'}])):
            with self.subTest(job=job), patch.object(s,'one',return_value={'jobs':[job]}):
                self.assertFalse(s.selector_active(self.t))
        for field, value in (('run_id',43),('run_attempt',2)):
            with self.subTest(field=field), patch.object(s,'one',return_value={'jobs':[self.selector(**{field:value})]}):
                with self.assertRaisesRegex(ValueError,'selector identity mismatch'):
                    s.selector_active(self.t)
        with patch.object(s,'one',return_value={'jobs':[self.selector(),self.selector(id=124)]}):
            with self.assertRaisesRegex(ValueError,'ambiguous parent selector'):
                s.selector_active(self.t)

    def test_selector_is_identified_by_group_and_grant_step(self):
        job = self.selector(name='Matrixone UT Coverage execution / Route Coverage group 0 / GROUP0')
        with patch.object(s,'one',return_value={'jobs':[job]}):
            self.assertTrue(s.selector_active(self.t))

    def test_parent_is_rechecked_after_active_selector(self):
        for changed in (dict(self.p,status='completed',conclusion='cancelled'),
                        dict(self.p,run_attempt=2),dict(self.p,head_sha='c'*40)):
            with self.subTest(changed=changed), \
                 patch.object(s,'run',side_effect=[dict(self.p,status='queued'),changed]), \
                 patch.object(s,'one',return_value={'jobs':[self.selector()]}):
                with self.assertRaises(ValueError):
                    s.parent_meta(self.t)

    def test_running_parent_waits_for_selector_metadata_before_checkout(self):
        responses = [{'jobs':[]}, {'jobs':[self.selector()]}]
        with patch.object(s, 'run', return_value=self.p) as read, \
             patch.object(s, 'one', side_effect=responses), patch.object(s.time, 'sleep') as sleep:
            self.assertEqual(s.parent_meta(self.t, require_selector=True), self.p)
            self.assertEqual(read.call_count, 3)
            sleep.assert_called_once_with(s.PARENT_POLL_SECONDS)

    def test_parent_metadata_errors_retry_only_when_transient(self):
        def http(code, headers=None):
            return urllib.error.HTTPError('https://api.github.com', code, 'injected', headers, None)
        cases = ((http(503), 5), (OSError('connection reset'), 5), (http(403), None),
                 (http(429, {'Retry-After':'7'}), 7), (http(429), 60),
                 (http(429, {'Retry-After':'Thu, 01 Jan 1970 00:16:47 GMT'}), 7),
                 (http(403, {'X-RateLimit-Remaining':'0', 'X-RateLimit-Reset':'1007'}), 7),
                 (http(403, {'X-RateLimit-Remaining':'0', 'X-RateLimit-Reset':'invalid'}), None),
                 (http(503, {'Retry-After':'7'}), 7), (http(429, {'Retry-After':'300'}), None),
                 (http(429, {'Retry-After':'invalid'}), None))
        for error, delay in cases:
            with self.subTest(error=error, delay=delay), patch.object(s, 'run', return_value=self.p), \
                 patch.object(s, 'one', side_effect=[error, {'jobs':[self.selector()]}]) as jobs, \
                 patch.object(s.time, 'time', return_value=1000), patch.object(s.time, 'sleep') as sleep:
                if delay is not None:
                    self.assertEqual(s.parent_meta(self.t, require_selector=True), self.p)
                    self.assertEqual(jobs.call_count, 2)
                    sleep.assert_called_once_with(delay)
                else:
                    with self.assertRaises(urllib.error.HTTPError):
                        s.parent_meta(self.t, require_selector=True)
                    jobs.assert_called_once()
                    sleep.assert_not_called()

    def test_persistent_parent_metadata_error_stops_at_poll_bound(self):
        error = urllib.error.HTTPError('https://api.github.com', 503, 'unavailable', None, None)
        with patch.object(s, 'run', return_value=self.p), patch.object(s, 'one', side_effect=error) as jobs, \
             patch.object(s, 'PARENT_POLLS', 2), patch.object(s.time, 'sleep') as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                s.parent_meta(self.t, require_selector=True)
            self.assertEqual(jobs.call_count, 2)
            sleep.assert_called_once_with(s.PARENT_POLL_SECONDS)

    def test_retry_rechecks_parent_identity_before_admission(self):
        error = urllib.error.HTTPError('https://api.github.com', 503, 'unavailable', None, None)
        with patch.object(s, 'run', side_effect=[self.p, dict(self.p, run_attempt=2)]), \
             patch.object(s, 'one', side_effect=error) as jobs, patch.object(s.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'identity mismatch: run_attempt'):
                s.parent_meta(self.t, require_selector=True)
            jobs.assert_called_once()
            sleep.assert_called_once_with(s.PARENT_POLL_SECONDS)

    def test_retry_restarts_parent_and_selector_validation(self):
        for reads in ([OSError('timeout'), self.p, self.p], [self.p, OSError('timeout'), self.p, self.p]):
            with self.subTest(reads=reads), patch.object(s, 'run', side_effect=reads) as parent, \
                 patch.object(s, 'one', return_value={'jobs':[self.selector()]}) as jobs, \
                 patch.object(s.time, 'sleep') as sleep:
                self.assertEqual(s.parent_meta(self.t, require_selector=True), self.p)
                self.assertEqual(parent.call_count, len(reads))
                self.assertEqual(jobs.call_count, len(reads) - 2)
                sleep.assert_called_once_with(s.PARENT_POLL_SECONDS)

    def test_parent_metadata_retry_respects_elapsed_deadline(self):
        with patch.object(s, 'run', side_effect=OSError('timeout')) as read, \
             patch.object(s.time, 'monotonic', side_effect=[0, 66]), patch.object(s.time, 'sleep') as sleep:
            with self.assertRaisesRegex(OSError, 'timeout'):
                s.parent_meta(self.t)
            read.assert_called_once()
            sleep.assert_not_called()

    def test_queued_parent_dispatch_requires_its_active_selector(self):
        env = dict(GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='42',GROUP='0',HEAD=self.sha,
                   SOURCE=self.src,PR='32922')
        assigned = dict(id=9,status='in_progress',runner_id=1,
                        runner_name='cs-jed-nc-n1-pr-v1-abc-runner-def')
        for active in (False, True):
            def metadata(path):
                if '/jobs?' in path:
                    return {'jobs':[self.selector()] if active else []}
                return {'object':{'sha':'b'*40}}

            with self.subTest(active=active), tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ,{**env,'RUNNER_TEMP':tmp}), \
                 patch.object(s,'run',return_value=dict(self.p,status='queued')), \
                 patch.object(s,'one',side_effect=metadata), patch.object(s,'dispatch',return_value=(51,(assigned,1))) as dispatch, \
                 patch.object(s,'output'), \
                 patch.object(s,'PARENT_POLLS',2), patch.object(s.time,'sleep'):
                if active:
                    s.choose()
                    dispatch.assert_called_once()
                    grant = json.loads(Path(tmp,'grant.json').read_text())
                    self.assertEqual((grant['child'],grant['ticket']['group']),(51,0))
                else:
                    with self.assertRaisesRegex(ValueError,'still not active'):
                        s.choose()
                    dispatch.assert_not_called()
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
                   dict(self.p,status='completed',conclusion='cancelled') if cancel else self.p,self.p]
            with self.subTest(cancel=cancel), patch.dict(os.environ,env), patch.object(s,'run',side_effect=reads), \
                 patch.object(s,'selector_active',side_effect=[False, True]), patch.object(s,'child_meta'), \
                 patch.object(s.time,'sleep'), patch.object(s,'one',return_value=pr), \
                 patch.object(s,'artifact',side_effect=[grant,proof]):
                if cancel:
                    with self.assertRaisesRegex(ValueError,'cancelled'):
                        s.admit()
                else:
                    s.admit()

    def test_queued_worker_checks_selector_before_checkout(self):
        grant = dict(child=51,ticket=self.t,pool='cs',nonce='test-nonce')
        proof = dict(allowed='true',verified_membership_http_status=204,pr_number='32922',
                     author='dev',head_repository=self.src,head_sha=self.sha)
        pr = dict(number=32922,user={'login':'dev'},head={'repo':{'full_name':self.src,'archived':False},
                  'sha':self.sha},state='open',draft=False,base={'repo':{'full_name':s.REPO},'ref':'main'},
                  labels=[{'name':'kind/test-ci'}])
        env = dict(TICKET=json.dumps(self.t),GROUP='0',HEAD=self.sha,SOURCE=self.src,POOL='cs',
                   GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='51')
        for parent_status in ('queued', 'in_progress'):
            for scenario in ('running', 'pending', 'failed', 'skipped', 'cleanup', 'cleanup_done', 'transient'):
                calls = 0

                def metadata(path):
                    nonlocal calls
                    if path.startswith('/pulls/'):
                        return pr
                    calls += 1
                    prior_checks = 2 if parent_status == 'queued' else 0
                    if scenario == 'transient' and calls == prior_checks + 1:
                        raise urllib.error.HTTPError('https://api.github.com', 503, 'unavailable', None, None)
                    if calls > prior_checks:
                        wait_status = ('completed' if scenario in ('failed', 'skipped') else
                                       'pending' if scenario in ('pending', 'cleanup', 'cleanup_done') else 'in_progress')
                        wait_conclusion = {'failed': 'failure', 'skipped': 'skipped'}.get(scenario)
                        steps = [
                            {'name': 'Release exactly one worker', 'conclusion': 'success'},
                            {'name': 'Wait for selected group, including its UT result',
                             'status': wait_status, 'conclusion': wait_conclusion},
                            {'name': 'Cancel the selected worker if the selector stops',
                             'status': ('completed' if scenario == 'cleanup_done' else
                                        'in_progress' if scenario in ('skipped', 'cleanup') else 'pending'),
                             'conclusion': 'success' if scenario == 'cleanup_done' else None},
                        ]
                        job = self.selector(steps=steps)
                    else:
                        job = self.selector()
                    return {'jobs':[job]}

                with self.subTest(parent_status=parent_status, scenario=scenario), patch.dict(os.environ,env), \
                     patch.object(s,'run',return_value=dict(self.p,status=parent_status)), \
                     patch.object(s,'child_meta'), patch.object(s.time,'sleep'), patch.object(s,'PARENT_POLLS',2), \
                     patch.object(s,'one',side_effect=metadata), patch.object(s,'artifact',side_effect=[grant,proof]):
                    if scenario in ('failed', 'skipped', 'cleanup', 'cleanup_done'):
                        with self.assertRaisesRegex(ValueError,'still not active'):
                            s.admit()
                    else:
                        s.admit()

    def test_admission_waits_for_own_identity_before_looking_up_grant(self):
        proof = dict(schema=2, allowed='true', verified_membership_http_status=204, pr_number='32922',
                     author='dev', head_repository=self.src, head_sha=self.sha, base_ref='main')
        pr = dict(number=32922, user={'login': 'dev'},
                  head={'repo': {'full_name': self.src, 'archived': False}, 'sha': self.sha},
                  state='open', draft=False, base={'repo': {'full_name': s.REPO}, 'ref': 'main'}, labels=[])
        env = dict(TICKET=json.dumps(self.t), GROUP='0', HEAD=self.sha, SOURCE=self.src, POOL='cs',
                   GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='51')
        def missing():
            return urllib.error.HTTPError('/actions/runs/51', 404, 'not found', None, None)

        for resolves in (True, False):
            now = 0
            interval = s.WORKER_METADATA_POLL_SECONDS
            reads = [missing(), self.worker if resolves else missing()]

            def sleep(seconds):
                nonlocal now
                now += seconds

            with self.subTest(resolves=resolves), patch.dict(os.environ, env), \
                 patch.object(s, 'parent_meta') as parent, patch.object(s, 'run', side_effect=reads) as get, \
                 patch.object(s, 'artifact') as artifact, patch.object(s, 'one', return_value=pr), \
                 patch.object(s, 'selector_job', return_value=None), patch.object(s, 'WORKER_ADMISSION_SECONDS', 2 * interval), \
                 patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s.time, 'sleep', side_effect=sleep):
                if resolves:
                    def read(t_parent, name, _filename):
                        self.assertEqual(t_parent, self.t['parent'])
                        self.assertEqual(get.call_count, len(reads))
                        return self.grant if name == 'coverage-admit-51' else proof

                    artifact.side_effect = read
                    s.admit()
                    self.assertEqual(artifact.call_count, 2)
                    self.assertEqual(parent.call_args.kwargs, {'require_selector': True})
                else:
                    with self.assertRaisesRegex(ValueError, 'no PR code executed'):
                        s.admit()
                    artifact.assert_not_called()
                self.assertEqual([call.args[0] for call in get.call_args_list], [51] * len(reads))
                self.assertEqual(now, interval if resolves else 2 * interval)

    def test_worker_started_first_can_obtain_the_later_selector_grant(self):
        env = dict(TICKET=json.dumps(self.t), GROUP='0', HEAD=self.sha, SOURCE=self.src, POOL='cs',
                   GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='51')
        proof = dict(schema=2, allowed='true', verified_membership_http_status=204, pr_number='32922',
                     author='dev', head_repository=self.src, head_sha=self.sha, base_ref='main')
        pr = dict(number=32922, user={'login': 'dev'}, state='open', draft=False,
                  head={'repo': {'full_name': self.src, 'archived': False}, 'sha': self.sha},
                  base={'repo': {'full_name': s.REPO}, 'ref': 'main'}, labels=[])
        for fails in (False, True):
            now = 0
            worker_reads, selector_reads, artifact_reads = [], [], []

            def sleep(seconds):
                nonlocal now
                now += seconds

            def run(run_id):
                if run_id == 42:
                    return self.p  # The parent stays active even if this group's selector failed.
                self.assertEqual(run_id, 51)
                worker_reads.append(now)
                if now < 75:  # after the old worker bound of 55s, before selector's 115s
                    raise urllib.error.HTTPError('/actions/runs/51', 404, 'unindexed', None, None)
                return self.worker

            def one(path):
                if path == '/pulls/32922':
                    return pr
                self.assertEqual(path, '/actions/runs/42/attempts/1/jobs?per_page=100&page=1')
                selector_reads.append(now)
                selector = self.selector()
                if fails and now >= 40:
                    selector.update(status='completed', conclusion='failure', steps=[
                        {'name': s.GRANT_STEP, 'conclusion': 'skipped'},
                    ])
                return {'jobs': [self.selector(group=1), selector]}

            def artifact(parent, name, filename):
                self.assertEqual(parent, 42)
                self.assertGreaterEqual(now, 75, 'read a grant before verifying the worker identity')
                artifact_reads.append((now, name))
                if name == 'coverage-admit-51':
                    return self.grant if now >= 85 else None
                self.assertEqual((name, filename), (s.proof_name(self.t), 'real-pr-authorization.json'))
                return proof

            with self.subTest(selector_fails=fails), patch.dict(os.environ, env), \
                 patch.object(s, 'run', side_effect=run), patch.object(s, 'one', side_effect=one), \
                 patch.object(s, 'artifact', side_effect=artifact), \
                 patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s.time, 'sleep', side_effect=sleep):
                if fails:
                    with self.assertRaisesRegex(ValueError, r'group 0 selector completed \(failure\).*no PR code executed'):
                        s.admit()
                    self.assertEqual(now, 60)
                    self.assertEqual(artifact_reads, [])
                else:
                    s.admit()
                    self.assertEqual(now, 85)
                    self.assertEqual(artifact_reads, [(75, 'coverage-admit-51'), (85, 'coverage-admit-51'),
                                                      (85, s.proof_name(self.t))])
                    self.assertEqual(selector_reads[-1], 85, 'grant must still be checked against a live selector')
                self.assertEqual(worker_reads[0], 0)
                self.assertEqual(worker_reads[-1], 60 if fails else 75)
                self.assertEqual(selector_reads[:3], [0, 30, 60])

    def test_first_cs_reservation_404_shares_the_dispatch_reconciliation_budget(self):
        cases = (
            ('ready after first 404', 0, 1, 2, None),
            ('last allowed poll can recover', 2, 1, 2, None),
            ('persistent 404', 0, 4, 4, (ValueError, 'attempt jobs lookup returned HTTP 404; no worker granted')),
            ('title and jobs share the same bound', 2, 2, 2,
             (ValueError, 'attempt jobs lookup returned HTTP 404; no worker granted')),
            ('forbidden jobs read', 0, 0, 1, (urllib.error.HTTPError, '403')),
            ('wrong job attempt', 0, 0, 1, (ValueError, 'wrong job identity')),
        )
        for name, delayed_title, missing_jobs, expected_jobs, error in cases:
            now = 0
            dispatched, run_reads, job_reads = [], [], []
            worker_job = dict(id=9, name='cs / Coverage reservation CS group 0', run_id=51, run_attempt=1,
                              status='in_progress', conclusion=None, labels=[s.POOLS['cs']], runner_id=3,
                              runner_name='cs-jed-nc-n1-pr-v1-abc-runner-def')

            def sleep(seconds):
                nonlocal now
                now += seconds

            def api(path, data=None):
                if data is not None:
                    self.assertEqual(path, f'/actions/workflows/{s.WORKER}/dispatches')
                    self.assertEqual(data['inputs']['pool'], 'cs')
                    dispatched.append(json.loads(data['inputs']['ticket']))
                    return {'workflow_run_id': 51}, now
                if path == '/actions/runs/42':
                    return self.p, now
                if path == f'/git/ref/heads/{s.WORKER_REF}':
                    return {'object': {'sha': self.t['revision']}}, now
                if path == '/actions/runs/51':
                    run_reads.append(now)
                    display = 'unindexed' if len(run_reads) <= delayed_title else s.title(dispatched[0], 'cs')
                    return dict(self.worker, display_title=display), now
                self.assertEqual(path, '/actions/runs/51/attempts/1/jobs?per_page=100')
                job_reads.append(now)
                if len(job_reads) <= missing_jobs:
                    raise urllib.error.HTTPError(path, 404, 'unindexed', None, None)
                if name == 'forbidden jobs read':
                    raise urllib.error.HTTPError(path, 403, 'forbidden', None, None)
                if name == 'wrong job attempt':
                    return {'total_count': 1, 'jobs': [dict(worker_job, run_attempt=2)]}, now
                return {'total_count': 1, 'jobs': [worker_job]}, now

            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                env = {**self.selector_env, 'RUNNER_TEMP': tmp, 'GITHUB_OUTPUT': str(Path(tmp, 'outputs')),
                       'COVERAGE_CS_SITE_RETRY': 'true'}
                with patch.dict(os.environ, env), patch.object(s, 'api', side_effect=api), \
                     patch.object(s, 'DISPATCH_METADATA_POLLS', 4), \
                     patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s.time, 'sleep', side_effect=sleep):
                    if error is None:
                        s.choose()
                        grant = json.loads(Path(tmp, 'grant.json').read_text())
                        self.assertEqual((grant['child'], grant['pool'], grant['ticket']), (51, 'cs', dispatched[0]))
                        self.assertEqual(s.digest(grant['nonce']), dispatched[0]['key'])
                    else:
                        with self.assertRaisesRegex(*error) as raised:
                            s.choose()
                        if isinstance(raised.exception, urllib.error.HTTPError):
                            raised.exception.close()
                        self.assertFalse(Path(tmp, 'grant.json').exists())
                        self.assertFalse(Path(tmp, 'outputs').exists())
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(len(job_reads), expected_jobs)
                self.assertEqual(len(run_reads), delayed_title + expected_jobs)
                self.assertEqual(job_reads[0], delayed_title * s.WORKER_METADATA_POLL_SECONDS)

    def test_ungranted_worker_stops_when_its_exact_selector_failed(self):
        env = dict(TICKET=json.dumps(self.t), GROUP='0', HEAD=self.sha, SOURCE=self.src, POOL='cs',
                   GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='51')
        terminal = self.selector(status='completed', conclusion='failure', steps=[
            {'name': s.GRANT_STEP, 'conclusion': 'skipped'},
        ])
        with patch.dict(os.environ, env), patch.object(s, 'run', return_value=self.p), \
             patch.object(s, 'child_meta'), patch.object(s.time, 'sleep') as sleep, \
             patch.object(s, 'artifact', return_value=None) as artifact, \
             patch.object(s, 'one', return_value={'jobs': [self.selector(group=1), terminal]}) as one:
            with self.assertRaisesRegex(ValueError, r'attempt 1 group 0 selector completed \(failure\).*no PR code executed'):
                s.admit()
            one.assert_called_once_with('/actions/runs/42/attempts/1/jobs?per_page=100&page=1')
            artifact.assert_called_once_with(42, 'coverage-admit-51', 'grant.json')
            sleep.assert_not_called()

    def test_selector_probe_never_grants_or_stops_a_live_group_for_a_sibling(self):
        env = dict(TICKET=json.dumps(self.t), GROUP='0', HEAD=self.sha, SOURCE=self.src, POOL='cs',
                   GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='51')
        for observation in (
            {'jobs': [self.selector(group=1, status='completed', conclusion='failure'), self.selector()]},
            {'jobs': [self.selector(status='completed', conclusion=None)]},
            {'jobs': [self.selector(status='completed', conclusion='failure', run_attempt=2)]},
            {'jobs': [self.selector(status='completed', conclusion='failure'), self.selector(id=124)]},
            urllib.error.HTTPError('https://api.github.com', 404, 'not yet indexed', None, None),
            urllib.error.HTTPError('https://api.github.com', 401, 'unreadable', None, None),
        ):
            now = 0

            def sleep(seconds):
                nonlocal now
                now += seconds

            with self.subTest(observation=observation), patch.dict(os.environ, env), \
                 patch.object(s, 'run', return_value=self.p), patch.object(s, 'child_meta'), \
                 patch.object(s.time, 'sleep', side_effect=sleep), patch.object(s.time, 'monotonic', side_effect=lambda: now), \
                 patch.object(s, 'WORKER_ADMISSION_SECONDS', 2 * s.GRANT_POLL_SECONDS), \
                 patch.object(s, 'artifact', return_value=None) as artifact, \
                 patch.object(s, 'one', side_effect=[observation]) as one:
                with self.assertRaisesRegex(ValueError, '^no grant; no PR code executed$'):
                    s.admit()
                self.assertEqual(artifact.call_count, 2)
                self.assertEqual(now, 2 * s.GRANT_POLL_SECONDS)
                one.assert_called_once()

    def test_selector_throttle_pauses_worker_reads_then_requires_the_exact_selector(self):
        env = dict(TICKET=json.dumps(self.t), GROUP='0', HEAD=self.sha, SOURCE=self.src, POOL='cs',
                   GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='51')
        proof = dict(schema=2, allowed='true', verified_membership_http_status=204, pr_number='32922',
                     author='dev', head_repository=self.src, head_sha=self.sha, base_ref='main')
        pr = dict(number=32922, user={'login': 'dev'}, state='open', draft=False,
                  head={'repo': {'full_name': self.src, 'archived': False}, 'sha': self.sha},
                  base={'repo': {'full_name': s.REPO}, 'ref': 'main'}, labels=[])
        for code, headers, cooldown, final_forbidden in (
            (403, {'X-RateLimit-Remaining': '4999'}, 60, False),
            (429, {'Retry-After': '75'}, 75, False),
            (403, {'X-RateLimit-Remaining': '4999'}, 60, True),
        ):
            now = 0
            request_times, selector_reads, sleeps = [], [], []

            def sleep(seconds):
                nonlocal now
                sleeps.append(seconds)
                now += seconds

            def run(run_id):
                request_times.append(now)
                return {42: self.p, 51: self.worker}[run_id]

            def one(path):
                request_times.append(now)
                if path == '/pulls/32922':
                    return pr
                self.assertEqual(path, '/actions/runs/42/attempts/1/jobs?per_page=100&page=1')
                selector_reads.append(now)
                if now == 0:
                    raise urllib.error.HTTPError(path, code, 'throttled', headers,
                        BytesIO(b'{"message":"You have exceeded a secondary rate limit."}'))
                if final_forbidden:
                    raise urllib.error.HTTPError(path, 403, 'forbidden', {'X-RateLimit-Remaining': '4998'},
                        BytesIO(b'{"message":"Resource not accessible by integration"}'))
                return {'jobs': [self.selector()]}

            def artifact(parent, name, filename):
                request_times.append(now)
                self.assertEqual(parent, 42)
                if name == 'coverage-admit-51':
                    return self.grant if now >= cooldown else None
                self.assertEqual((name, filename), (s.proof_name(self.t), 'real-pr-authorization.json'))
                return proof

            with self.subTest(code=code, cooldown=cooldown, final_forbidden=final_forbidden), \
                 patch.dict(os.environ, env), patch.object(s, 'run', side_effect=run), \
                 patch.object(s, 'one', side_effect=one), patch.object(s, 'artifact', side_effect=artifact), \
                 patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s.time, 'sleep', side_effect=sleep):
                if final_forbidden:
                    with self.assertRaisesRegex(urllib.error.HTTPError, '403: forbidden') as exc:
                        s.admit()
                    exc.exception.close()
                else:
                    s.admit()
                self.assertEqual(selector_reads, [0, cooldown])
                self.assertEqual(set(request_times), {0, cooldown})
                self.assertEqual(sleeps, [cooldown])

    def test_repeated_selector_throttle_backs_off_without_extending_admission_deadline(self):
        now = 0
        sleeps, selector_reads = [], []

        def sleep(seconds):
            nonlocal now
            sleeps.append(seconds)
            now += seconds

        def selector(path):
            selector_reads.append(now)
            raise urllib.error.HTTPError(path, 429, 'throttled', None, None)

        with patch.object(s, 'WORKER_ADMISSION_SECONDS', 130), patch.object(s, 'parent_meta'), \
             patch.object(s, 'reconcile_child_meta'), patch.object(s, 'admission_artifact', return_value=None) as grant, \
             patch.object(s, 'one', side_effect=selector), patch.object(s.time, 'monotonic', side_effect=lambda: now), \
             patch.object(s.time, 'sleep', side_effect=sleep):
            with self.assertRaisesRegex(ValueError, '^no grant; no PR code executed$'):
                s.wait_for_grant(self.t, 51, 'cs')
            self.assertEqual(selector_reads, [0, 60])
            self.assertEqual(grant.call_count, 2)
            self.assertEqual(sleeps, [60, 70])
            self.assertEqual(now, 130)

    def test_admission_backoff_grows_until_read_succeeds(self):
        env = dict(TICKET=json.dumps(self.t), GROUP='0', HEAD=self.sha, SOURCE=self.src, POOL='cs',
                   GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='51')
        proof = dict(schema=2, allowed='true', verified_membership_http_status=204, pr_number='32922',
                     author='dev', head_repository=self.src, head_sha=self.sha, base_ref='main')
        pr = dict(number=32922, user={'login': 'dev'}, state='open', draft=False,
                  head={'repo': {'full_name': self.src, 'archived': False}, 'sha': self.sha},
                  base={'repo': {'full_name': s.REPO}, 'ref': 'main'}, labels=[])
        for permission_error in (False, True):
            now = 0
            parent_reads, selector_reads, sleeps = [], [], []
            throttled = 'You have exceeded a secondary rate limit.'

            def error(path, message):
                return urllib.error.HTTPError(path, 403, 'Forbidden', {'X-RateLimit-Remaining': '4999'},
                                              BytesIO(json.dumps({'message': message}).encode()))

            def sleep(seconds):
                nonlocal now
                sleeps.append(seconds)
                now += seconds

            def run(run_id):
                if run_id == 51:
                    return self.worker
                self.assertEqual(run_id, 42)
                parent_reads.append(now)
                if 0 < now < 75:
                    raise error('/actions/runs/42', 'Resource not accessible by integration' if permission_error else throttled)
                return self.p

            def one(path):
                if path == '/pulls/32922':
                    if now < 240:
                        raise urllib.error.HTTPError(path, 429, 'throttled', {'Retry-After': '60'}, None)
                    return pr
                self.assertEqual(path, '/actions/runs/42/attempts/1/jobs?per_page=100&page=1')
                selector_reads.append(now)
                if now < 75:
                    raise error(path, throttled)
                return {'jobs': [self.selector()]}

            def artifact(parent, name, filename):
                self.assertEqual(parent, 42)
                if name == 'coverage-admit-51':
                    return self.grant if now >= 75 else None
                self.assertEqual((name, filename), (s.proof_name(self.t), 'real-pr-authorization.json'))
                return proof

            with self.subTest(permission_error=permission_error), patch.dict(os.environ, env), \
                 patch.object(s, 'run', side_effect=run), patch.object(s, 'one', side_effect=one), \
                 patch.object(s, 'artifact', side_effect=artifact), \
                 patch.object(s, 'WORKER_ADMISSION_SECONDS', 300), \
                 patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s.time, 'sleep', side_effect=sleep):
                if permission_error:
                    with self.assertRaisesRegex(urllib.error.HTTPError, '403: Forbidden') as denied:
                        s.admit()
                    denied.exception.close()
                    self.assertEqual(now, 60)
                    self.assertEqual(parent_reads, [0, 0, 60])
                    self.assertEqual(selector_reads, [0])
                    self.assertEqual(sleeps, [60])
                else:
                    s.admit()
                    self.assertEqual(now, 240)
                    self.assertEqual(parent_reads, [0, 0, 60, 180, 240, 240])
                    self.assertEqual(selector_reads, [0, 240])
                    self.assertEqual(sleeps, [60, 120, 60])

    def test_admission_deadline_bounds_parent_and_selector_io_during_artifact_retry(self):
        parent_path = '/actions/runs/42'
        selector_path = '/actions/runs/42/attempts/1/jobs?per_page=100&page=1'
        for slow in ('parent', 'selector'):
            now = 0
            late_reads, artifact_reads = [], []

            def sleep(seconds):
                nonlocal now
                now = 1130 if now == 0 else now + seconds

            def open_request(req, timeout):
                nonlocal now
                path = req.full_url.removeprefix(s.API)
                self.assertIn(path, (parent_path, selector_path))
                if now >= 1135:
                    late_reads.append((path, now, timeout))
                    if len(late_reads) == 1:
                        now += 25 if slow == 'parent' else 2
                    else:
                        now += 25
                payload = (dict(self.p, status='queued') if now >= 1135 else self.p) if path == parent_path else {
                    'jobs': [self.selector()]}
                response = BytesIO(json.dumps(payload).encode())
                response.headers = {'Date': 'Thu, 01 Jan 1970 00:00:00 GMT'}
                return response

            def artifact(parent, name, filename):
                artifact_reads.append(now)
                if now == 1130:
                    raise urllib.error.HTTPError('https://api.github.com', 503, 'unavailable', None, None)
                return None

            with self.subTest(slow=slow), patch.dict(os.environ, {'ACTIONS_TOKEN': 'test-token'}), \
                 patch.object(s, 'reconcile_child_meta'), patch.object(s, 'artifact', side_effect=artifact), \
                 patch.object(s.urllib.request, 'build_opener') as opener, \
                 patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s.time, 'sleep', side_effect=sleep):
                opener.return_value.open.side_effect = open_request
                with self.assertRaisesRegex((ValueError, RuntimeError), 'admission deadline exceeded.*no PR code executed'):
                    s.wait_for_grant(self.t, 51, 'cs')
                expected = [(parent_path, 1135, 5)]
                if slow == 'selector':
                    expected.append((selector_path, 1137, 3))
                self.assertEqual(late_reads, expected)
                self.assertEqual(artifact_reads, [0, 1130])

    def test_final_parent_validation_rechecks_selector_after_throttle(self):
        now = 0
        selector_reads = []

        def sleep(seconds):
            nonlocal now
            now += seconds

        def run(run_id):
            self.assertEqual(run_id, self.t['parent'])
            if selector_reads == [0] and now == 0:
                raise urllib.error.HTTPError('/actions/runs/42', 429, 'throttled', {'Retry-After': '60'}, None)
            return self.p

        def jobs(path):
            self.assertEqual(path, '/actions/runs/42/attempts/1/jobs?per_page=100&page=1')
            selector_reads.append(now)
            job = self.selector() if now == 0 else self.selector(status='completed', conclusion='failure')
            return {'jobs': [job]}

        with patch.object(s, 'run', side_effect=run), patch.object(s, 'one', side_effect=jobs), \
             patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s.time, 'sleep', side_effect=sleep), \
             s.admission_window():
            with self.assertRaisesRegex(ValueError, 'still not active'):
                s.parent_meta(self.t, require_selector=True)
            self.assertEqual(selector_reads[:2], [0, 60])

    def test_late_artifact_redirect_cannot_start_a_signed_download(self):
        now = 0
        name = 'coverage-admit-51'

        def zip_redirect(path):
            nonlocal now
            self.assertEqual(path, '/actions/artifacts/7/zip')
            now = 10
            raise urllib.error.HTTPError(path, 302, 'Found', {'Location': 'https://example.invalid/signed'}, None)

        listing = {'artifacts': [{'id': 7, 'name': name, 'expired': False, 'size_in_bytes': 512}]}
        with patch.object(s, 'WORKER_ADMISSION_SECONDS', 10), patch.object(s.time, 'monotonic', side_effect=lambda: now), \
             patch.object(s, 'one', return_value=listing), patch.object(s, 'api', side_effect=zip_redirect), \
             patch.object(s.urllib.request, 'urlopen') as download, s.admission_window():
            with self.assertRaisesRegex(s.AdmissionDeadlineExceeded, 'no PR code executed'):
                s.artifact(42, name, 'grant.json')
            download.assert_not_called()

    def test_grant_does_not_reset_the_deadline_for_final_authorization(self):
        env = dict(TICKET=json.dumps(self.t), GROUP='0', HEAD=self.sha, SOURCE=self.src, POOL='cs',
                   GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='51')
        now = 0

        def grant(*_args):
            nonlocal now
            now = 9

        def proof(*_args):
            nonlocal now
            self.assertEqual(now, 9)
            now = 11
            return {'schema': 2, 'allowed': 'true'}

        with patch.dict(os.environ, env), patch.object(s, 'WORKER_ADMISSION_SECONDS', 10), \
             patch.object(s.time, 'monotonic', side_effect=lambda: now), patch.object(s, 'wait_for_grant', side_effect=grant), \
             patch.object(s, 'artifact', side_effect=proof) as read, patch.object(s, 'one') as live_pr, \
             patch.object(s, 'parent_meta') as final_selector:
            with self.assertRaisesRegex(s.AdmissionDeadlineExceeded, 'no PR code executed'):
                s.admit()
            read.assert_called_once_with(42, s.proof_name(self.t), 'real-pr-authorization.json')
            live_pr.assert_not_called()
            final_selector.assert_not_called()

    def test_admission_artifact_retries_transient_reads_and_checks_parent(self):
        for code in (404, 408, 503):
            with self.subTest(code=code), patch.object(s, 'parent_meta') as parent, \
                 patch.object(s.time, 'sleep') as sleep, patch.object(s, 'artifact', side_effect=[
                    urllib.error.HTTPError('https://api.github.com', code, 'temporary', None, None),
                    self.grant,
                 ]) as read:
                self.assertEqual(s.admission_artifact(self.t, 'coverage-admit-51', 'grant.json'), self.grant)
                parent.assert_called_once_with(self.t)
                sleep.assert_called_once_with(s.PARENT_POLL_SECONDS)
                self.assertEqual([call.args for call in read.call_args_list], [(42, 'coverage-admit-51', 'grant.json')] * 2)

    def test_admission_artifact_read_is_bounded_and_does_not_retry_invalid_payload(self):
        for error, reads, pattern in (
            (urllib.error.HTTPError('https://api.github.com', 404, 'absent', None, None),
             s.ADMISSION_ARTIFACT_POLLS, 'no PR code executed'),
            (urllib.error.HTTPError('https://api.github.com', 429, 'rate limit', {'Retry-After': '60'}, None),
             1, 'no PR code executed'),
            (urllib.error.HTTPError('https://api.github.com', 403, 'forbidden', None, None), 1, '403'),
            (ValueError('invalid control artifact payload'), 1, 'invalid control artifact payload'),
        ):
            with self.subTest(error=error), patch.object(s, 'parent_meta') as parent, \
                 patch.object(s.time, 'sleep') as sleep, patch.object(s, 'artifact', side_effect=error) as read:
                with self.assertRaisesRegex((ValueError, urllib.error.HTTPError), pattern):
                    s.admission_artifact(self.t, 'coverage-admit-51', 'grant.json')
                self.assertEqual(read.call_count, reads)
                self.assertEqual(parent.call_count, reads - 1)
                self.assertEqual(sleep.call_count, reads - 1)

    def test_revoke_only_the_worker_owned_by_the_selector(self):
        for scenario in ('active', 'completed', 'wrong_child', 'wrong_attempt', 'wrong_nonce',
                         'wrong_worker', 'unexpected_status'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                local_grant = json.loads(json.dumps(self.grant))
                metadata = dict(self.worker)
                if scenario == 'completed':
                    metadata['status'] = 'completed'
                elif scenario == 'wrong_child':
                    local_grant['child'] = 52
                elif scenario == 'wrong_attempt':
                    local_grant['ticket']['attempt'] = 2
                elif scenario == 'wrong_nonce':
                    local_grant['nonce'] = 'wrong'
                elif scenario == 'wrong_worker':
                    metadata['display_title'] = 'unrelated worker'
                elif scenario == 'unexpected_status':
                    metadata['status'] = 'unknown'
                Path(tmp, 'grant.json').write_text(json.dumps(local_grant))
                with patch.dict(os.environ, {**self.selector_env, 'RUNNER_TEMP':tmp}), \
                     patch.object(s, 'run', return_value=metadata), patch.object(s, 'api') as api:
                    if scenario in ('active', 'completed'):
                        s.revoke()
                        if scenario == 'active':
                            api.assert_called_once_with('/actions/runs/51/cancel', {})
                        else:
                            api.assert_not_called()
                    else:
                        with self.assertRaises(ValueError):
                            s.revoke()
                        api.assert_not_called()

    def test_admitted_worker_is_cancelled_when_selector_fails(self):
        proof = dict(allowed='true', verified_membership_http_status=204, pr_number='32922',
                     author='dev', head_repository=self.src, head_sha=self.sha)
        pr = dict(number=32922, user={'login':'dev'}, state='open', draft=False,
                  head={'repo':{'full_name':self.src, 'archived':False}, 'sha':self.sha},
                  base={'repo':{'full_name':s.REPO}, 'ref':'main'}, labels=[{'name':'kind/test-ci'}])
        common = dict(GITHUB_RUN_ATTEMPT='1', GROUP='0', PR='32922', HEAD=self.sha, SOURCE=self.src)
        worker_env = dict(common, GITHUB_RUN_ID='51', TICKET=json.dumps(self.t), POOL='cs')

        def read_run(run_id):
            return self.p if run_id == 42 else self.worker

        def read_metadata(path):
            return pr if path.startswith('/pulls/') else {'jobs':[self.selector()]}

        with tempfile.TemporaryDirectory() as tmp, patch.object(s, 'run', side_effect=read_run), \
             patch.object(s, 'one', side_effect=read_metadata), \
             patch.object(s, 'artifact', side_effect=[self.grant,proof]), patch.object(s, 'api') as api:
            Path(tmp, 'grant.json').write_text(json.dumps(self.grant))
            with patch.dict(os.environ, worker_env):
                s.admit()
            with patch.dict(os.environ, {**self.selector_env, 'RUNNER_TEMP':tmp}):
                s.revoke()
            api.assert_called_once_with('/actions/runs/51/cancel', {})

    def test_revoke_retries_transient_errors_and_handles_completion_race(self):
        for status in (500, 409):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'grant.json').write_text(json.dumps(self.grant))
                error = urllib.error.HTTPError('https://api.github.com', status, 'injected', None, None)
                metadata = [self.worker, dict(self.worker, status='completed')] if status == 409 else [self.worker] * 2
                responses = [error] if status == 409 else [error, ({}, 0)]
                with patch.dict(os.environ, {**self.selector_env, 'RUNNER_TEMP':tmp}), \
                     patch.object(s, 'run', side_effect=metadata), patch.object(s, 'api', side_effect=responses) as api, \
                     patch.object(s.time, 'sleep') as sleep:
                    s.revoke()
                    self.assertEqual(api.call_count, len(responses))
                    sleep.assert_called_once_with(2)

    def test_revoke_retries_lookup_and_surfaces_cancellation_failure(self):
        for scenario in ('lookup_transient', 'cancel_denied', 'cancel_exhausted'):
            error_code = 403 if scenario == 'cancel_denied' else 500
            error = urllib.error.HTTPError('https://api.github.com', error_code, 'injected', None, None)
            metadata = [error, self.worker] if scenario == 'lookup_transient' else [self.worker] * 3
            response = None if scenario == 'lookup_transient' else error
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'grant.json').write_text(json.dumps(self.grant))
                with patch.dict(os.environ, {**self.selector_env, 'RUNNER_TEMP':tmp}), \
                     patch.object(s, 'run', side_effect=metadata), patch.object(s, 'api', side_effect=response) as api, \
                     patch.object(s.time, 'sleep') as sleep:
                    if scenario == 'lookup_transient':
                        s.revoke()
                        api.assert_called_once_with('/actions/runs/51/cancel', {})
                        sleep.assert_called_once_with(s.PARENT_POLL_SECONDS)
                    else:
                        with self.assertRaises(urllib.error.HTTPError):
                            s.revoke()
                        self.assertEqual(api.call_count, 1 if scenario == 'cancel_denied' else 3)
                        self.assertEqual(sleep.call_count, 0 if scenario == 'cancel_denied' else 2)


    def test_receipt_base_is_pinned_for_both_pools_and_retry(self):
        self.assertEqual(s.RECEIPT_BASE_REFS, {"main", RELEASE_BASE})
        for attempt, pool, site in ((1, "cs", None), (2, "cs", "ruh"), (2, "idc", None)):
            t = dict(self.t, attempt=attempt, group=attempt-1, title_version=2)
            if site:
                t["site"] = site
            grant = dict(child=51, ticket=t, pool=pool, nonce="test-nonce")
            child = dict(id=51, repository={"full_name": s.REPO}, run_attempt=1,
                         path=".github/workflows/"+s.WORKER, event="workflow_dispatch",
                         head_branch=s.WORKER_REF, head_sha=t["revision"], display_title=s.short_title(t))
            marker = dict(run_id=51, run_attempt=1, status="completed", conclusion="skipped",
                          name=s.title(t, pool)+s.IDENTITY_SUFFIX)
            env = dict(TICKET=json.dumps(t), GROUP=str(t["group"]), HEAD=self.sha, SOURCE=self.src,
                       POOL=pool, GITHUB_RUN_ATTEMPT="1", GITHUB_RUN_ID="51")
            if site:
                env["RUNNER_NAME"] = f"cs-{site}-nc-n0-pr-v1-abc-runner-def"
            for schema, authorized, live, member, labels, draft, expected in (
                    (2, RELEASE_BASE, RELEASE_BASE, 204, ["kind/refactor"], False, True),
                    (2, RELEASE_BASE, RELEASE_BASE, 204, [], True, True),
                    (2, RELEASE_BASE, RELEASE_BASE, 404, ["kind/test-ci"], False, False),
                    (2, "main", "main", 204, [], False, True),
                    (2, "main", "main", 204, [], True, True),
                    (2, "main", "main", 404, ["kind/test-ci"], True, False),
                    (2, "4.2-dev", "main", 204, [], True, False),
                    (2, "main", "4.2-dev", 204, [], True, False),
                    (2, "4.1-dev", "4.1-dev", 204, [], True, False),
                    (2, None, "main", 204, [], True, False),
                    (1, None, "main", 204, ["kind/test-ci"], False, True),
                    (1, None, "main", 204, ["kind/test-ci"], True, False),
                    (1, None, "main", 204, [], False, False),
                    (1, None, RELEASE_BASE, 204, ["kind/test-ci"], False, False),
                    (1, RELEASE_BASE, RELEASE_BASE, 204, ["kind/test-ci"], False, False)):
                proof = dict(schema=schema, allowed="true", verified_membership_http_status=member, pr_number="32922",
                             author="dev", head_repository=self.src, head_sha=self.sha)
                if authorized is not None:
                    proof["base_ref"] = authorized
                pr = dict(number=32922, user={"login": "dev"},
                          head={"repo": {"full_name": self.src, "archived": False}, "sha": self.sha},
                          state="open", draft=draft, base={"repo": {"full_name": s.REPO}, "ref": live},
                          labels=[{"name": label} for label in labels])
                def read_run(run_id):
                    return {42: dict(self.p, run_attempt=attempt, status="queued"), 51: child}[run_id]
                def read(path):
                    selector = self.selector(group=t["group"], run_attempt=attempt,
                                             name=f"Matrixone UT Coverage execution / Route Coverage group {t['group']} / {s.short_title(t)}")
                    return {f"/actions/runs/42/attempts/{attempt}/jobs?per_page=100&page=1": {"jobs": [selector]},
                            "/actions/runs/51/attempts/1/jobs?per_page=100": {"total_count": 1, "jobs": [marker]},
                            "/pulls/32922": pr}[path]
                with self.subTest(attempt=attempt, pool=pool, site=site, schema=schema, authorized=authorized,
                                  live=live, member=member, labels=labels, draft=draft), \
                     patch.dict(os.environ, env), patch.object(s, "run", side_effect=read_run), \
                     patch.object(s, "one", side_effect=read), \
                     patch.object(s, "artifact", side_effect=[grant, proof]) as artifact:
                    if expected:
                        s.admit()
                    else:
                        with self.assertRaisesRegex(ValueError, "PR no longer matches authorization"):
                            s.admit()
                    self.assertEqual(artifact.call_args.args[1], s.proof_name(t))


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
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RUNNER_TEMP': tmp}), \
                 patch.object(s,'parent_meta'), patch.object(s,'api',side_effect=post), \
                 patch.object(s,'one',side_effect=[{'object':{'sha':x}} for x in (rev1,rev2)]), \
                 patch.object(s,'run',side_effect=metadata), patch.object(s,'observe',return_value=(None, 0)) as observe:
                r, rev, nonce, first = s.reserve(t, pool, float("inf"))
            self.assertEqual(events, ['dispatch','cancel','confirmed','dispatch'])
            self.assertEqual((r,rev,s.digest(nonce)),(102,rev2,dispatched[1]['key']))
            self.assertNotEqual(dispatched[0]['key'],dispatched[1]['key'])
            if pool == 'cs':
                observe.assert_called_once_with(102, t, 'cs')
                self.assertEqual(first, (None, 0))
            else:
                observe.assert_not_called()
                self.assertIsNone(first)

    def test_revision_retry_fails_closed(self):
        t = dict(parent=1, attempt=1, group=0, head='a'*40)
        for error, count in [(ValueError('identity mismatch'),0),
                             (s.DispatchRevisionChanged(101,'c'*40),1)]:
            with patch.object(s,'one',return_value={'object':{'sha':'b'*40}}), \
                 patch.object(s,'parent_meta'), patch.object(s,'dispatch',side_effect=error) as send, \
                 patch.object(s,'child_meta',return_value={'status':'queued'}), \
                 patch.object(s,'api') as post, patch.object(s.time,'sleep'):
                with self.assertRaises(ValueError):
                    s.reserve(t,'idc',float('inf'))
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

    def test_first_pool_plan_is_fresh_distinct_and_accounts_for_pending_work(self):
        now = 1000
        m0, m1, r0 = ("cs-mnl2-nc-n0-pr-v1", "cs-mnl2-nc-n1-pr-v1", "cs-ruh-nc-n0-pr-v1")
        rows = [dict(pool=m0, slots=3, pending=1), dict(pool=m1, slots=2, pending=1),
                dict(pool=r0, slots=1, pending=0)]
        receipt = lambda choices, captured=now: json.dumps(dict(captured_at=captured, pools=choices))
        self.assertEqual(s.first_cs_pool_plan(receipt(rows), now), (m0, r0))
        self.assertEqual(s.first_cs_pool_plan(receipt([dict(rows[0], pending=3), rows[1]]), now), (m1,))
        for bad in (None, "{}", receipt(rows, captured=now - 181), receipt(rows, captured=now + 1),
                    receipt(rows + [rows[0]]), receipt([dict(rows[0], slots=True)]),
                    receipt([dict(rows[0], pool="ubuntu-latest")])):
            self.assertEqual(s.first_cs_pool_plan(bad, now), ())

    def test_first_pool_is_ignored_without_exact_isolated_pin(self):
        target = "cs-ruh-nc-n0-pr-v1"
        ticket = {"pr": 32922, "attempt": 1, "group": 0}
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_WORKFLOW_REF":
               f"{s.REPO}/.github/workflows/coverage-standby-pr32922.yaml@refs/heads/weilu-runner-test"}
        with patch.dict(os.environ, env), patch.object(s, "first_cs_pool_plan", return_value=(target,)):
            self.assertEqual(s.choose_initial_scale_set(ticket), target)
            self.assertIsNone(s.choose_initial_scale_set(dict(ticket, group=1)))
            self.assertIsNone(s.choose_initial_scale_set(dict(ticket, pr=33401)))
            self.assertIsNone(s.choose_initial_scale_set(dict(ticket, attempt=2)))
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}):
                self.assertIsNone(s.choose_initial_scale_set(ticket))

    def test_first_pool_requires_exact_label_and_assigned_runner_prefix(self):
        pool = "cs-mnl2-nc-n2-pr-v1"
        ticket = {"group": 0, "scale_set": pool}
        name = "Coverage reservation CS group 0"
        row = dict(self.active, labels=[pool], runner_name=pool + "-abc-runner-def",
                   name=name, run_id=12, run_attempt=1)
        with patch.object(s, "api", return_value=({"total_count": 1, "jobs": [row]}, 2)):
            self.assertEqual(s.observe(12, ticket, "cs"), (row, 2))
        for wrong in (dict(row, labels=[s.POOLS["cs"]]),
                      dict(row, runner_name="cs-mnl2-nc-n0-pr-v1-abc-runner-def")):
            with patch.object(s, "api", return_value=({"total_count": 1, "jobs": [wrong]}, 2)):
                with self.assertRaisesRegex(ValueError, "outside requested CS pool|wrong worker label"):
                    s.observe(12, ticket, "cs")
        for invalid in ("ubuntu-latest", [], 5):
            with self.assertRaisesRegex(ValueError, "invalid exact CS pool"):
                s.worker_labels("cs", scale_set=invalid)

    def test_exact_pool_freshness_at_dispatch_matches_receipt_grant_and_output(self):
        target = "cs-ruh-nc-n0-pr-v1"
        snapshot = json.dumps(dict(captured_at=1000, pools=[dict(pool=target, slots=1, pending=0)]))
        for post_age, expected in ((180, target), (183, None)):
            with self.subTest(post_age=post_age), tempfile.TemporaryDirectory() as tmp:
                wall = [1179]  # Fresh when choose() starts.
                sent = []

                def parent_meta(ticket):
                    if "key" in ticket:  # Last identity read before dispatch, or final grant check.
                        wall[0] = 1300 if sent else 1000 + post_age

                def post(path, data):
                    self.assertEqual(path, f"/actions/workflows/{s.WORKER}/dispatches")
                    self.assertEqual(wall[0], 1000 + post_age)
                    sent.append(json.loads(data["inputs"]["ticket"]))
                    return {"workflow_run_id": 111}, wall[0]

                env = dict(self.env, RUNNER_TEMP=tmp, GITHUB_EVENT_NAME="push",
                           GITHUB_WORKFLOW_REF=f"{s.REPO}/.github/workflows/coverage-standby-pr32922.yaml@refs/heads/weilu-runner-test",
                           COVERAGE_CS_FIRST_POOL_SNAPSHOT=snapshot)
                with patch.dict(os.environ, env), patch.object(s.time, "time", side_effect=lambda: wall[0]), \
                     patch.object(s, "parent_meta", side_effect=parent_meta), patch.object(s, "api", side_effect=post), \
                     patch.object(s, "one", return_value={"object": {"sha": "b"*40}}), \
                     patch.object(s, "reconcile_child_meta"), patch.object(s, "output") as output, \
                     patch.object(s, "observe", return_value=(dict(self.active, runner_name=target+"-abc-runner-def"), 0)):
                    s.choose()
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0].get("scale_set"), expected)
                for filename in ("reservation.json", "grant.json"):
                    self.assertEqual(json.loads(Path(tmp, filename).read_text())["ticket"], sent[0])
                placement = {"scale_set": expected} if expected else {}
                output.assert_called_once_with(child=111, pool="cs", site="", **placement)

    def test_expired_exact_target_restores_shared_queue_site_retry(self):
        ticket = dict(parent=42, group=0, scale_set="cs-ruh-nc-n0-pr-v1")
        first_site = s.CS_SITES[ticket["parent"] % len(s.CS_SITES)]

        def reserve(t, pool, deadline):
            if "site" not in t:
                del t["scale_set"]  # The receipt expired before the first POST.
                return 111, "b"*40, "first", (self.q, 50)
            self.assertNotIn("scale_set", t)
            self.assertEqual(t["site"], first_site)
            job = dict(self.active, runner_name=f"cs-{first_site}-nc-n0-pr-v1-abc-runner-def")
            return 222, "c"*40, "second", (job, 351)

        with patch.object(s, "reserve", side_effect=reserve), patch.object(s.time, "sleep"), \
             patch.object(s, "observe", side_effect=[(self.q, 350), (self.q, 3650)]), \
             patch.object(s, "observe_cs_handoff", return_value=(self.q, 350)), \
             patch.object(s, "withdraw_cs") as withdraw:
            result = s.select_cs(ticket, True, float("inf"))
        withdraw.assert_called_once()
        self.assertEqual(withdraw.call_args.args[0], 111)
        self.assertEqual(result, (222, "c"*40, "second", "cs", 50))

    def test_exact_first_dispatch_does_not_rotate_and_shanghai_deadline_is_unchanged(self):
        target = "cs-ruh-nc-n0-pr-v1"
        deadline = 10 + s.SELECTOR_RESERVATION_SECONDS
        calls = []
        def reserve(ticket, pool, received_deadline):
            self.assertEqual(received_deadline, deadline)
            calls.append(("reserve", pool, ticket.get("scale_set")))
            return (111, "b"*40, "a", (self.q, 50)) if pool == "cs" else (222, "c"*40, "b", None)
        def withdraw(r, ticket, revision, received_deadline):
            self.assertEqual(received_deadline, deadline)
            calls.append(("confirmed_cancel", r, ticket["scale_set"]))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            **self.env, "RUNNER_TEMP": tmp, "COVERAGE_CS_SITE_RETRY": "true",
        }), patch.object(s, "choose_initial_scale_set", return_value=target), patch.object(s, "parent_meta"), \
             patch.object(s, "child_meta"), patch.object(s, "reserve", side_effect=reserve), \
             patch.object(s, "withdraw_cs", side_effect=withdraw), patch.object(s, "output"), \
             patch.object(s.time, "monotonic", return_value=10), patch.object(s.time, "sleep"), \
             patch.object(s, "observe", side_effect=[
                 (self.q, 350), (self.q, 3649), (self.q, 3650), (self.q, 3651),
             ]) as observe:
            s.choose()
            self.assertEqual(observe.call_count, 4)
            grant = json.loads(Path(tmp, "grant.json").read_text())
        self.assertEqual(calls, [("reserve", "cs", target), ("confirmed_cancel", 111, target),
                                 ("reserve", "idc", None)])
        self.assertEqual((grant["child"], grant["pool"]), (222, "idc"))
        self.assertNotIn("scale_set", grant["ticket"])

    def scenario(self, observations, cancellation, count, selected, *, attempt='1', revisions=None):
        import tempfile, os, json
        from pathlib import Path
        if revisions is None:
            revisions = ['b'*40] * (1 + int(cancellation))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**self.env, 'GITHUB_RUN_ATTEMPT': attempt, 'RUNNER_TEMP': tmp}), \
             patch.object(s,'parent_meta',return_value={'head_sha':'a'*40,'head_repository':{'full_name':'fork/matrixone'}}), \
             patch.object(s,'one',side_effect=[{'object':{'sha':sha}} for sha in revisions]), \
             patch.object(s,'require_previous_grants_terminal') as previous, \
             patch.object(s,'dispatch',side_effect=[(111,observations[0]),(222,None)]) as launch, \
             patch.object(s,'observe',side_effect=observations[1:]), \
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

    def test_shanghai_dispatch_recovers_ref_read_and_revokes_a_late_receipt(self):
        deadline = s.SELECTOR_RESERVATION_SECONDS
        for late in (False, True):
            now = 0
            tickets = {}
            events = []

            def select(t, *_):
                nonlocal now
                now = deadline - 40
                t.update(revision="b"*40, key=s.digest("nonce"))
                tickets[111] = dict(t)
                return 111, "b"*40, "nonce", "idc", 1

            def get(path):
                if path == "/actions/runs/222/attempts/1/jobs?per_page=100":
                    events.append("identity")
                    marker = dict(run_id=222, run_attempt=1, status="completed", conclusion="skipped",
                                  name=s.title(tickets[222], "idc") + s.IDENTITY_SUFFIX)
                    return {"total_count": 1, "jobs": [marker]}
                self.assertEqual(path, "/git/ref/heads/main")
                events.append("ref")
                if events.count("ref") == 1 and not late:
                    raise urllib.error.HTTPError(path, 503, "temporary failure", None, None)
                return {"object": {"sha": "c"*40}}

            def sleep(delay):
                nonlocal now
                now += delay

            def metadata(run_id):
                ticket = tickets[run_id]
                cancelled = run_id == 111 and "withdraw" in events
                if run_id == 222:
                    events.append("verify")
                    if late and events.count("verify") == 1:
                        raise urllib.error.HTTPError("/actions/runs/222", 404, "not indexed", None, None)
                return dict(id=run_id, repository={"full_name": s.REPO}, run_attempt=1,
                            path=".github/workflows/" + s.WORKER, event="workflow_dispatch",
                            head_branch=s.WORKER_REF, head_sha=ticket["revision"],
                            display_title=s.title(ticket, "cs") if run_id == 111 else s.short_title(ticket),
                            status="completed" if cancelled else "queued", conclusion="cancelled" if cancelled else None)

            def post(path, data):
                nonlocal now
                if path == "/actions/runs/111/cancel":
                    events.append("withdraw")
                    return {}, 0
                if path == "/actions/runs/222/cancel":
                    events.append("revoke")
                    return {}, 0
                self.assertEqual(path, f"/actions/workflows/{s.WORKER}/dispatches")
                self.assertLess(now, deadline)
                self.assertEqual(data["inputs"]["pool"], "idc")
                events.append("dispatch")
                tickets[222] = json.loads(data["inputs"]["ticket"])
                if late:
                    now = deadline + 1
                return {"workflow_run_id": 222}, 0

            with self.subTest(late=late), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = {**self.env, "RUNNER_TEMP": tmp, "GITHUB_OUTPUT": str(root / "outputs"), "CHILD": ""}
                with patch.dict(os.environ, env), patch.object(s.time, "monotonic", side_effect=lambda: now), \
                     patch.object(s.time, "sleep", side_effect=sleep), patch.object(s, "select_cs", side_effect=select), \
                     patch.object(s, "observe", return_value=(self.q, 3601)), patch.object(s, "run", side_effect=metadata), \
                     patch.object(s, "parent_meta"), patch.object(s, "one", side_effect=get), \
                     patch.object(s, "api", side_effect=post):
                    if late:
                        with self.assertRaisesRegex(ValueError, "dispatch metadata window elapsed"):
                            s.choose()
                        self.assertFalse((root / "grant.json").exists())
                        self.assertFalse((root / "outputs").exists())
                        self.assertEqual(events, ["withdraw", "ref", "dispatch"])
                        s.revoke()
                        self.assertEqual(events, ["withdraw", "ref", "dispatch", "verify", "verify", "identity", "revoke"])
                    else:
                        s.choose()
                        grant = json.loads((root / "grant.json").read_text())
                        self.assertEqual((grant["child"], grant["ticket"]), (222, tickets[222]))
                        self.assertEqual(events, ["withdraw", "ref", "ref", "dispatch", "verify", "identity"])

    def test_expired_shanghai_reservation_does_not_dispatch(self):
        now = 0
        deadline = s.SELECTOR_RESERVATION_SECONDS

        def select(*_):
            nonlocal now
            now = deadline - 30
            return 111, "b"*40, "nonce", "idc", 1

        def withdraw(*_):
            nonlocal now
            now = deadline - 5

        def branch_ref(path):
            nonlocal now
            self.assertEqual(path, "/git/ref/heads/main")
            now += 20
            return {"object": {"sha": "c"*40}}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**self.env, "RUNNER_TEMP": tmp}), \
             patch.object(s.time, "monotonic", side_effect=lambda: now), \
             patch.object(s, "select_cs", side_effect=select), patch.object(s, "withdraw_cs", side_effect=withdraw), \
             patch.object(s, "observe", return_value=(self.q, 3601)), patch.object(s, "child_meta"), \
             patch.object(s, "parent_meta"), patch.object(s, "one", side_effect=branch_ref), \
             patch.object(s, "reconcile_child_meta"), \
             patch.object(s, "api", return_value=({"workflow_run_id": 222}, 0)) as send:
            with self.assertRaisesRegex(TimeoutError, "worker ref: phase deadline exceeded"):
                s.choose()
            self.assertFalse(Path(tmp, "grant.json").exists())
        send.assert_not_called()

    def test_rerun_uses_attempt_bound_grant_and_fresh_shanghai_revision(self):
        self.scenario([(self.q,1),(self.q,3601),(self.q,3602)],True,2,('idc',222),
                      attempt='2', revisions=['b'*40,'c'*40])

    def test_assigned_during_final_read_is_never_cancelled(self):
        self.scenario([(self.q,1),(self.q,3601),(self.active,3602)],False,1,('cs',111))

    def test_queue_and_handoff_recover_without_redispatch(self):
        unavailable = urllib.error.HTTPError("/jobs", 503, "not available", None, None)
        unindexed = s.WorkerIdentityPending("attempt jobs returned HTTP 404")
        self.scenario([(self.q, 1), unavailable, (self.q, 3601), unindexed, (self.active, 3602)],
                      False, 1, ('cs', 111))

    def test_unsettled_cs_reservation_hits_selection_deadline(self):
        now = [0]
        observation = (dict(self.q, status="pending"), 0)
        with patch.object(s.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(s.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)), \
             patch.object(s, "reserve", return_value=(111, "b"*40, "nonce", observation)) as reserve, \
             patch.object(s, "observe", return_value=observation) as observe:
            with self.assertRaisesRegex(TimeoutError, r"CS reservation 111: selection deadline exceeded \(pending\)"):
                s.select_cs(dict(parent=42, group=0), False, deadline=65)
        self.assertEqual(now[0], 65)
        self.assertEqual(observe.call_count, 2)
        reserve.assert_called_once()

    def test_withdrawal_waits_for_cancellation_after_a_read_failure(self):
        path = "/actions/runs/111"
        unavailable = urllib.error.HTTPError(path, 503, "not available", None, None)
        snapshots = [unavailable, {"status": "queued"}, {"status": "completed", "conclusion": "cancelled"}]
        with patch.object(s, "api") as post, patch.object(s, "child_meta", side_effect=snapshots) as read, \
             patch.object(s.time, "sleep") as sleep:
            s.withdraw_cs(111, {}, "b"*40, deadline=s.time.monotonic() + 60)
        post.assert_called_once_with(path + "/cancel", {})
        self.assertEqual([call.args[0] for call in read.call_args_list], [111] * 3)
        self.assertEqual(sleep.call_count, 2)

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
             patch.object(s,'dispatch',return_value=(111,(self.q,1))), \
             patch.object(s,'observe',side_effect=[(self.q,3601)]+[(transitional,3602)]*3), \
             patch.object(s,'child_meta'), patch.object(s,'api') as api, patch.object(s.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'did not settle'):
                s.choose()
            api.assert_not_called()

    def test_immediate_assignment(self):
        self.scenario([(self.active,2)],False,1,('cs',111))

    def run_site_sweep(self, observations, *, cancel_code=None, after_cancel="cancelled",
                       expected_error=None, enabled=True):
        events = []
        withdrawn = set()
        rev = "b" * 40
        snapshots = iter(observations)

        def reserve(ticket, pool, deadline):
            run_id = 1 + sum(event[0] == "reserve" for event in events)
            events.append(("reserve", run_id, pool, ticket.get("site")))
            ticket.update(revision=rev, key=s.digest(f"test-nonce-{run_id}"))
            return run_id, rev, f"test-nonce-{run_id}", next(snapshots) if pool == "cs" else None

        def cancel(path, data):
            run_id = int(path.split("/")[-2])
            self.assertEqual(data, {})
            events.append(("cancel", run_id))
            withdrawn.add(run_id)
            if cancel_code is not None:
                raise urllib.error.HTTPError(path, cancel_code, "injected", {}, None)

        def metadata(run_id, ticket, pool, revision):
            self.assertEqual(pool, "cs")
            self.assertEqual(revision, rev)
            self.assertEqual(ticket["key"], s.digest(f"test-nonce-{run_id}"))
            if run_id in withdrawn:
                events.append(("confirm", run_id))
                if isinstance(after_cancel, Exception):
                    raise after_cancel
                if after_cancel == "in_progress":
                    return dict(status="in_progress", conclusion=None)
                return dict(status="completed", conclusion=after_cancel)
            return dict(status="queued")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                **self.env, "RUNNER_TEMP": tmp, "COVERAGE_CS_SITE_RETRY": "true" if enabled else "false",
        }), patch.object(s, "reserve", side_effect=reserve), patch.object(s, "observe", side_effect=lambda *_: next(snapshots)), \
             patch.object(s, "parent_meta"), patch.object(s, "child_meta", side_effect=metadata), \
             patch.object(s, "api", side_effect=cancel), patch.object(s, "output"), patch.object(s.time, "sleep"):
            if expected_error is not None:
                with self.assertRaisesRegex(*expected_error):
                    s.choose()
                self.assertFalse(Path(tmp, "grant.json").exists())
                return None, events
            s.choose()
            return json.loads(Path(tmp, "grant.json").read_text()), events

    def test_stalled_shared_queue_tries_one_site_after_confirmed_cancellation(self):
        grant, events = self.run_site_sweep([(self.q, 1), (self.q, 301), (self.q, 302), (self.active, 321)])
        self.assertEqual(events, [("reserve", 1, "cs", None), ("cancel", 1), ("confirm", 1),
                                  ("reserve", 2, "cs", "mnl2")])
        self.assertEqual((grant["child"], grant["ticket"]["site"], grant["pool"]), (2, "mnl2", "cs"))

    def test_site_retry_does_not_cancel_when_runner_appears_in_final_read(self):
        for tail in ([(self.active, 302)],
                     [(dict(self.q, runner_id=2), 302), (self.active, 333)]):
            with self.subTest(tail=tail):
                grant, events = self.run_site_sweep([(self.q, 1), (self.q, 301), *tail])
                self.assertEqual(events, [("reserve", 1, "cs", None)])
                self.assertEqual(grant["child"], 1)

    def test_site_retry_does_not_replace_unconfirmed_cancellation(self):
        grant, events = self.run_site_sweep([(self.q, 1), (self.q, 301), (self.q, 302)],
                                            after_cancel="failure",
                                            expected_error=(ValueError, "did not acknowledge withdrawal"))
        self.assertEqual(events, [("reserve", 1, "cs", None), ("cancel", 1), ("confirm", 1)])
        self.assertIsNone(grant)

    def test_site_sweep_reconciles_cancel_conflict_before_replacement(self):
        queue = [(self.q, 1), (self.q, 301), (self.q, 302)]
        grant, events = self.run_site_sweep([*queue, (self.active, 321)], cancel_code=409)
        self.assertEqual(events, [("reserve", 1, "cs", None), ("cancel", 1), ("confirm", 1),
                                  ("reserve", 2, "cs", "mnl2")])
        self.assertEqual(grant["child"], 2)
        for code, status, error in (
                (409, "in_progress", (ValueError, "cancellation not confirmed")),
                (409, "success", (ValueError, "did not acknowledge withdrawal")),
                (409, ValueError("identity mismatch"), (ValueError, "identity mismatch")),
                (403, "cancelled", (urllib.error.HTTPError, "403"))):
            with self.subTest(code=code, status=status):
                grant, events = self.run_site_sweep(queue, cancel_code=code,
                                                    after_cancel=status, expected_error=error)
                self.assertIsNone(grant)
                self.assertEqual([e for e in events if e[0] != "confirm"],
                                 [("reserve", 1, "cs", None), ("cancel", 1)])
                self.assertEqual(("confirm", 1) in events, code == 409)

    def test_unsuccessful_site_sweep_returns_to_shared_queue_and_keeps_shanghai_deadline(self):
        observations = [(self.q, 1), (self.q, 301), (self.q, 302)]
        for queued in (310, 410, 510, 610):
            observations.extend((self.q, now) for now in (queued, queued + 90, queued + 91))
        observations.extend((self.q, now) for now in (710, 3600, 3601, 3602))
        grant, events = self.run_site_sweep(observations)
        expected = [("reserve", 1, "cs", None)]
        for run_id, site in enumerate(("mnl2", "ruh", "mty", "jed", None), 2):
            expected += [("cancel", run_id-1), ("confirm", run_id-1), ("reserve", run_id, "cs", site)]
        expected += [("cancel", 6), ("confirm", 6), ("reserve", 7, "idc", None)]
        self.assertEqual(events, expected)
        self.assertEqual((grant["child"], grant["pool"]), (7, "idc"))
        self.assertNotIn("site", grant["ticket"])

    def test_default_off_does_not_retry_other_sites(self):
        grant, events = self.run_site_sweep([(self.q, 1), (self.q, 301), (self.active, 322)], enabled=False)
        self.assertEqual(events, [("reserve", 1, "cs", None)])
        self.assertEqual(grant["child"], 1)

    def test_targeted_placement_requires_both_labels_and_the_matching_site(self):
        ticket = {"group": 0, "site": "ruh"}
        row = dict(self.active, name="cs / Coverage reservation CS group 0", run_id=12, run_attempt=1,
                   labels=["cs-ruh-node-cache-all", s.POOLS["cs"]])
        for changes, match in (
                (dict(runner_name="cs-ruh-nc-n0-pr-v1-abc-runner-def"), True),
                ({}, False),
                (dict(labels=[s.POOLS["cs"]]), False)):
            worker = dict(row, **changes)
            with self.subTest(changes=changes), patch.object(s, "api", return_value=(
                    {"total_count": 1, "jobs": [worker]}, 2)):
                if match:
                    self.assertEqual(s.observe(12, ticket, "cs"), (worker, 2))
                else:
                    with self.assertRaisesRegex(ValueError, "outside requested CS site|wrong worker label"):
                        s.observe(12, ticket, "cs")
        with self.assertRaisesRegex(ValueError, "invalid CS site"):
            s.worker_labels("cs", "other")

    def test_empty_dispatch_response_reconciles_on_same_worker_ref_without_second_post(self):
        t = dict(parent=1, attempt=1, group=1, head='a'*40, key='b'*64)
        remember = Mock()
        with patch.object(s, 'WORKER_REF', 'staging'), patch.object(s, 'parent_meta'), \
             patch.object(s, 'child_meta'), patch.object(s, 'api', return_value=({},0)) as post, \
             patch.object(s, 'observe', return_value=(None, 0)), \
             patch.object(s, 'one', return_value={'workflow_runs':[{'id':123,'display_title':s.title(t,'cs')}]}) as get:
            self.assertEqual(s.dispatch(t,'cs','c'*40,float('inf'), remember=remember),(123,(None,0)))
            remember.assert_called_once_with(123)
            self.assertEqual(post.call_count,1)
            self.assertEqual(post.call_args.args[1]['ref'],'staging')
            self.assertIn('branch=staging',get.call_args.args[0])

    def test_proof_is_specific_to_retry_group_and_attempt(self):
        t = dict(parent=42, group=1, attempt=1)
        self.assertEqual(s.proof_name(t), "real-pr-snapshot-42-attempt-1")
        self.assertEqual(s.proof_name(dict(t,attempt=2)), "real-pr-retry-42-attempt-2-group-1")

    def test_live_old_grant_blocks_only_its_group(self):
        t = dict(parent=42,group=0,attempt=2,head='a'*40)
        old = dict(id=81, run_attempt=1, display_title=s.title(dict(t,attempt=1,key='a'*64), 'cs'),
                   path='.github/workflows/'+s.WORKER, event='workflow_dispatch',
                   repository={'full_name':s.REPO},head_branch=s.WORKER_REF,status='in_progress')
        def read(path):
            if path == '/actions/runs/81/attempts/1':
                return old
            self.assertEqual(path, '/actions/runs/42/artifacts?per_page=100&page=1')
            return {'artifacts': [{'name': 'coverage-admit-81'}]}

        with patch.object(s, 'one', side_effect=read), \
             patch.object(s, 'run', return_value=dict(old, run_attempt=2, status='completed')) as latest:
            with self.assertRaisesRegex(ValueError,'still active'):
                s.require_previous_grants_terminal(t)
            s.require_previous_grants_terminal(dict(t,group=1))
            old['status'] = 'completed'
            s.require_previous_grants_terminal(t)
            latest.assert_not_called()

            old['run_attempt'] = 2
            with self.assertRaisesRegex(ValueError, 'unrecognized previous grant'):
                s.require_previous_grants_terminal(t)

    def test_previous_short_title_grant_retries_only_pending_identity(self):
        t = dict(parent=42, group=0, attempt=2, head='a'*40)
        title = s.title(dict(t, attempt=1, key='a'*64), 'cs')
        old = dict(id=81, run_attempt=1, display_title='GROUP0', path='.github/workflows/'+s.WORKER,
                   event='workflow_dispatch', repository={'full_name':s.REPO}, head_branch=s.WORKER_REF,
                   status='completed')
        marker = dict(name=title+s.IDENTITY_SUFFIX, run_id=81, run_attempt=1,
                      status='completed', conclusion='skipped')
        jobs = '/actions/runs/81/attempts/1/jobs?per_page=100'

        def http(code):
            return urllib.error.HTTPError(jobs, code, 'unavailable', None, None)

        for name, responses, expected_error in (
            ('404 then ready', [http(404), [marker]], None),
            ('not indexed then ready', [[], [marker]], None),
            ('persistent 404', [http(404), http(404)], (ValueError, 'previous grant.*not verified')),
            ('permission denied', [http(403)], (urllib.error.HTTPError, '403')),
            ('wrong attempt', [[dict(marker, run_attempt=2)]], (ValueError, 'invalid worker identity marker')),
        ):
            results = iter(responses)

            def one(path):
                if '/artifacts?' in path:
                    return {'artifacts': [{'name': 'coverage-admit-81'}]}
                if path == '/actions/runs/81/attempts/1':
                    return old
                self.assertEqual(path, jobs)
                result = next(results)
                if isinstance(result, Exception):
                    raise result
                return {'jobs': result, 'total_count': len(result)}

            with self.subTest(name=name), patch.object(s, 'one', side_effect=one) as read, \
                 patch.object(s, 'run', return_value=dict(old, run_attempt=2)), patch.object(s, 'PARENT_POLLS', 2), \
                 patch.object(s.time, 'sleep') as sleep:
                if expected_error is None:
                    s.require_previous_grants_terminal(t)
                else:
                    with self.assertRaisesRegex(*expected_error):
                        s.require_previous_grants_terminal(t)
                self.assertEqual([c.args[0] for c in read.call_args_list], [
                    '/actions/runs/42/artifacts?per_page=100&page=1',
                    *['/actions/runs/81/attempts/1', jobs] * len(responses),
                ])
                self.assertEqual(sleep.call_count, len(responses) - 1)

    def test_failed_only_job_boundary(self):
        from pathlib import Path
        wf = (Path(__file__).resolve().parents[1]/'.github/workflows/coverage-queue-handoff.yaml').read_text()
        selector, collector = wf.split('  collect:\n')
        for name in (s.GRANT_STEP, s.WAIT_STEP, s.REVOKE_STEP):
            self.assertEqual(selector.count(f'name: {name}\n'), 1)
        self.assertLess(selector.index('coverage_standby.py choose'), selector.index('coverage_standby.py wait'))
        revoke = selector.split(f'name: {s.REVOKE_STEP}\n')[1]
        self.assertIn("always() && steps.wait.outcome != 'success'", revoke)
        self.assertIn("steps.select.outputs.child != '' || steps.select.outcome == 'failure'", revoke)
        self.assertIn('timeout-minutes: 2', revoke)
        self.assertIn('coverage_standby.py revoke', revoke)
        self.assertIn('real-pr-retry-', selector)
        self.assertNotIn('coverage_standby.py wait', collector)
        self.assertIn('actions: read', collector)
        self.assertGreater(85*60, s.WAIT)


class Progress(unittest.TestCase):
    def test_wait_recovers_status_on_the_original_attempt(self):
        path = "/actions/runs/123/attempts/1"
        ready = dict(id=123, status="completed", run_attempt=1, conclusion="success")
        errors = [urllib.error.HTTPError(path, code, "not available", None, None) for code in (404, 503)]
        with patch.dict(os.environ, dict(CHILD="123", GROUP="1", POOL="cs")), \
             patch.object(s, "one", side_effect=[*errors, ready]) as read, \
             patch.object(s.time, "sleep") as sleep:
            s.wait()
        self.assertEqual([call.args for call in read.call_args_list], [(path,)] * 3)
        self.assertEqual(sleep.call_count, 2)

    def test_worker_read_honors_rate_limit_and_phase_deadline(self):
        path = "/actions/runs/123/attempts/1"
        for code, headers, deadline, success, expected_sleep in (
            (429, {"Retry-After": "60"}, 85, True, [60]),
            (429, {"Retry-After": "60"}, 50, False, []),
            (403, {}, 85, False, []),
            (503, {}, 24, False, [5, 10]),
        ):
            now = [0]
            errors = [urllib.error.HTTPError(path, code, "not available", headers, None)
                      for _ in range(1 if success else 3)]
            responses = errors + ([{"status": "completed"}] if success else [])
            with self.subTest(code=code, deadline=deadline), patch.object(s.time, "monotonic", side_effect=lambda: now[0]), \
                 patch.object(s.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)) as sleep, \
                 patch.object(s, "one", side_effect=responses) as read:
                if success:
                    self.assertEqual(s.read_worker("worker result", s.one, path, deadline=deadline),
                                     {"status": "completed"})
                else:
                    expected_error = urllib.error.HTTPError if code == 403 else TimeoutError
                    with self.assertRaises(expected_error):
                        s.read_worker("worker result", s.one, path, deadline=deadline)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], expected_sleep)
                self.assertEqual([call.args for call in read.call_args_list], [(path,)] * (len(expected_sleep) + 1))

    def test_worker_read_rejects_response_after_handoff_deadline(self):
        with patch.object(s.time, "monotonic", side_effect=[0, 11]), \
             patch.object(s, "one", return_value={"status": "completed"}) as read, \
             patch.object(s.time, "sleep") as sleep:
            with self.assertRaisesRegex(TimeoutError, "handoff: phase deadline exceeded"):
                s.read_worker("handoff", s.one, "/runs/123", deadline=10)
        read.assert_called_once()
        sleep.assert_not_called()

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
                 patch.object(s, "worker_attempt", side_effect=[dict(id=123, run_attempt=1, status="in_progress"),
                     dict(id=123, status="completed", run_attempt=1, conclusion=conclusion)]), \
                 patch.object(s, "worker_progress", side_effect=OSError("do not print API details")), \
                 patch.object(s.time, "sleep"), patch("builtins.print") as out:
                if conclusion == "success":
                    s.wait()
                else:
                    with self.assertRaisesRegex(ValueError, "selected coverage group did not pass"):
                        s.wait()
                self.assertIn("worker details temporarily unavailable", str(out.call_args_list))
                self.assertNotIn("do not print API details", str(out.call_args_list))

    def test_job_details_are_rate_limited_but_child_result_is_polled_each_time(self):
        interval = s.JOB_DETAIL_INTERVAL
        times = [0, interval-1, interval, interval+1]
        finish = dict(id=123, status="completed", run_attempt=1, conclusion="success")
        with patch.dict(os.environ, dict(CHILD="123", GROUP="1", POOL="cs")), \
             patch.object(s, "worker_attempt", side_effect=[
                 dict(id=123, run_attempt=1, status="in_progress")] * len(times) + [finish]) as runs, \
             patch.object(s.time, "monotonic", side_effect=times), patch.object(s.time, "sleep"), \
             patch.object(s, "worker_progress", return_value=("running", s.run_url(123))) as details, \
             patch("builtins.print"):
            s.wait()
        self.assertEqual(runs.call_count, len(times)+1)
        self.assertEqual(details.call_count, 2)


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
        # These tests isolate identity reconciliation; the selector test above
        # exercises the real first attempt-jobs read and passes its snapshot onward.
        self.first_observation = (None, 0)
        observation = patch.object(s, 'observe', return_value=self.first_observation)
        self.addCleanup(observation.stop)
        observation.start()
        self.remember = Mock()

    def dispatch(self, deadline=float("inf")):
        return s.dispatch(self.ticket, "cs", "b"*40, deadline, remember=self.remember)

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
            self.assertEqual(self.dispatch(), (51, self.first_observation))
            post.assert_called_once()

    def test_dispatch_requests_run_id_and_checks_the_returned_worker(self):
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
             patch.object(s, "one", return_value={"total_count": 1, "jobs": [self.marker]}) as lookup, \
             patch.object(s, "run", return_value=self.meta):
            self.assertEqual(self.dispatch(), (51, self.first_observation))
            post.assert_called_once()
            self.assertIs(post.call_args.args[1]["return_run_details"], True)
            lookup.assert_called_once_with("/actions/runs/51/attempts/1/jobs?per_page=100")

    def test_dispatch_rechecks_deadline_after_parent_validation(self):
        with patch.object(s, "parent_meta") as parent, patch.object(s.time, "monotonic", return_value=10), \
             patch.object(s, "api") as post:
            with self.assertRaisesRegex(ValueError, "deadline exceeded before dispatch"):
                self.dispatch(deadline=10)
        parent.assert_called_once_with(self.ticket)
        post.assert_not_called()
        self.remember.assert_not_called()

    def test_late_dispatch_response_does_not_restart_selection_budget(self):
        with patch.object(s, "parent_meta"), patch.object(s.time, "monotonic", side_effect=[9, 11, 11]), \
             patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
             patch.object(s, "reconcile_child_meta") as lookup:
            with self.assertRaisesRegex(ValueError, "dispatch metadata window elapsed"):
                self.dispatch(deadline=10)
        post.assert_called_once()
        lookup.assert_not_called()
        self.remember.assert_called_once_with(51)

    def test_dispatch_waits_through_run_404_and_unready_titles_on_the_same_id(self):
        ready = dict(self.meta, display_title=s.title(self.ticket, "cs"))
        pending = dict(ready, display_title="Coverage standby single-group worker")
        untitled = {key: value for key, value in ready.items() if key != "display_title"}
        lookup_error = urllib.error.HTTPError("/actions/runs/51", 404, "not found", None, None)
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
             patch.object(s, "one", side_effect=[lookup_error, untitled, pending, ready]) as get, \
             patch.object(s.time, "sleep") as sleep, patch("builtins.print") as log:
            self.assertEqual(self.dispatch(), (51, self.first_observation))
        self.assertEqual([call.args[0] for call in get.call_args_list], ["/actions/runs/51"] * 4)
        post.assert_called_once()
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(any("HTTP 404" in call.args[0] for call in log.call_args_list))
        self.assertFalse(any(pending["display_title"] in call.args[0] for call in log.call_args_list))

    def test_dispatch_retries_transient_attempt_jobs_reads_on_returned_id(self):
        path = "/actions/runs/51/attempts/1/jobs?per_page=100"
        errors = [urllib.error.HTTPError(path, code, "not available", None, None) for code in (404, 503)]
        ready = {"total_count": 1, "jobs": [self.marker]}
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
             patch.object(s, "run", return_value=self.meta) as run, patch.object(s, "one", side_effect=[*errors, ready]) as get, \
             patch.object(s.time, "sleep") as sleep:
            self.assertEqual(self.dispatch(), (51, self.first_observation))
        self.assertEqual([call.args[0] for call in get.call_args_list], [path] * 3)
        self.assertEqual([call.args[0] for call in run.call_args_list], [51] * 3)
        post.assert_called_once()
        self.assertEqual([call.args for call in sleep.call_args_list], [(5,)] * 2)

    def test_selector_does_not_publish_a_grant_after_persistent_404_or_wrong_title(self):
        for problem in ("run 404", "wrong long title"):
            def run_metadata(run_id):
                self.assertEqual(run_id, 51)
                if problem == "run 404":
                    raise urllib.error.HTTPError("/actions/runs/51", 404, "not found", None, None)
                # A canonical-looking title from a different parent cannot grant this worker.
                return dict(self.meta, display_title=s.title(dict(self.ticket, parent=43), "cs"))

            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as root:
                env = dict(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1", GROUP="0", HEAD="a"*40,
                           SOURCE="contributor/matrixone", PR="33174", COVERAGE_CS_SITE_RETRY="false",
                           RUNNER_TEMP=root, GITHUB_OUTPUT=str(Path(root, "outputs")))
                with patch.dict(os.environ, env), patch.object(s, "parent_meta"), patch.object(s, "observe") as observe, \
                     patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
                     patch.object(s, "one", return_value={"object": {"sha": "b"*40}}) as branch, \
                     patch.object(s, "run", side_effect=run_metadata) as run, patch.object(s.time, "sleep") as sleep:
                    with self.assertRaisesRegex(ValueError, r"worker 51 metadata not verified.*no worker granted"):
                        s.choose()
                self.assertEqual(run.call_count, s.DISPATCH_METADATA_POLLS)
                self.assertEqual(sleep.call_count, s.DISPATCH_METADATA_POLLS - 1)
                post.assert_called_once()
                self.assertTrue(post.call_args.args[0].endswith("/dispatches"))
                branch.assert_called_once_with("/git/ref/heads/main")
                observe.assert_not_called()
                self.assertFalse(Path(root, "grant.json").exists())
                self.assertFalse(Path(root, "outputs").exists())

    def test_dispatch_does_not_retry_forbidden_or_invalid_identity(self):
        wrong_marker = dict(self.marker, name=s.title(dict(self.ticket, parent=43), "cs") + s.IDENTITY_SUFFIX)
        for reason in ("run 403", "wrong run envelope", "wrong short-title marker", "wrong revision"):
            meta = dict(self.meta)
            if reason == "wrong run envelope":
                meta["head_branch"] = "other"
            if reason == "wrong revision":
                meta.update(head_sha="e"*40, display_title=s.title(self.ticket, "cs"))
            result = urllib.error.HTTPError("/actions/runs/51", 403, "forbidden", None, None) if reason == "run 403" else meta
            with self.subTest(reason=reason), patch.object(s, "parent_meta"), \
                 patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
                 patch.object(s, "run", side_effect=[result]) as run, patch.object(s, "one", return_value={
                     "jobs": [wrong_marker], "total_count": 1}), patch.object(s.time, "sleep") as sleep:
                if reason == "run 403":
                    with self.assertRaises(urllib.error.HTTPError) as exc:
                        self.dispatch()
                    self.assertEqual(exc.exception.code, 403)
                    exc.exception.close()
                elif reason == "wrong revision":
                    with self.assertRaises(s.DispatchRevisionChanged):
                        self.dispatch()
                else:
                    with self.assertRaisesRegex(ValueError, "dedicated worker identity mismatch"):
                        self.dispatch()
                run.assert_called_once_with(51)
                post.assert_called_once()
                sleep.assert_not_called()

    def test_dispatch_204_sticks_with_previously_verified_id_after_404(self):
        ready = dict(self.meta, display_title=s.title(self.ticket, "cs"))
        missing = urllib.error.HTTPError("/actions/runs/51", 404, "not found", None, None)
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({}, 0)) as post, \
             patch.object(s, "one", return_value={"workflow_runs": [ready]}) as find, \
             patch.object(s, "run", side_effect=[missing, ready]) as run, patch.object(s.time, "sleep") as sleep:
            self.assertEqual(self.dispatch(), (51, self.first_observation))
        post.assert_called_once()
        find.assert_called_once()
        self.assertEqual([call.args[0] for call in run.call_args_list], [51, 51])
        sleep.assert_called_once_with(5)

    def test_dispatch_retries_a_queued_marker_for_the_returned_id(self):
        queued = dict(self.marker, status="queued", conclusion=None)
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
             patch.object(s, "one", side_effect=[{"total_count": 1, "jobs": [m]} for m in (queued, self.marker)]), \
             patch.object(s, "run", return_value=self.meta), patch.object(s.time, "sleep") as sleep:
            self.assertEqual(self.dispatch(), (51, self.first_observation))
            post.assert_called_once()
            sleep.assert_called_once_with(5)
        for changes in (dict(run_id=90), dict(run_attempt=2), dict(conclusion="success")):
            with self.subTest(changes=changes), patch.object(s, "one", return_value={
                     "total_count": 1, "jobs": [{**queued, **changes}]}):
                with self.assertRaisesRegex(ValueError, "invalid worker identity marker") as err:
                    s.identity_title(51, self.meta)
                self.assertNotIsInstance(err.exception, s.WorkerIdentityPending)

    def test_204_dispatch_ignores_unindexed_same_title_but_rejects_verified_duplicates(self):
        other = dict(self.meta, id=52)
        for state in ("missing", "queued", "vanished", "service unavailable", "forbidden", "duplicate"):
            def read(path):
                if "/actions/workflows/" in path:
                    return {"workflow_runs": [other, self.meta]}
                if "/52/" in path:
                    errors = {"vanished": 404, "service unavailable": 503, "forbidden": 403}
                    if state in errors:
                        raise urllib.error.HTTPError(path, errors[state], state, None, None)
                    if state == "missing":
                        return {"total_count": 0, "jobs": []}
                    marker = dict(self.marker, run_id=52)
                    if state == "queued":
                        marker.update(status="queued", conclusion=None)
                    return {"total_count": 1, "jobs": [marker]}
                return {"total_count": 1, "jobs": [self.marker]}

            with self.subTest(state=state), patch.object(s, "parent_meta"), \
                 patch.object(s, "api", return_value=({}, 0)) as post, patch.object(s, "one", side_effect=read), \
                 patch.object(s, "run", return_value=self.meta) as metadata, patch.object(s.time, "sleep") as sleep:
                if state == "duplicate":
                    with self.assertRaisesRegex(ValueError, "duplicate dispatch"):
                        self.dispatch()
                    metadata.assert_not_called()
                elif state == "forbidden":
                    with self.assertRaisesRegex(urllib.error.HTTPError, "403"):
                        self.dispatch()
                    metadata.assert_not_called()
                else:
                    self.assertEqual(self.dispatch(), (51, self.first_observation))
                    metadata.assert_called_once_with(51)
                post.assert_called_once()
                sleep.assert_not_called()
