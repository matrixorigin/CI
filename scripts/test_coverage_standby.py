import unittest
import json
import os
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch
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
                 patch.object(s,'one',side_effect=metadata), patch.object(s,'dispatch',return_value=51) as dispatch, \
                 patch.object(s,'observe',return_value=(assigned,1)), patch.object(s,'output'), \
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
                        sleep.assert_called_once_with(2)
                    else:
                        with self.assertRaises(urllib.error.HTTPError):
                            s.revoke()
                        self.assertEqual(api.call_count, 1 if scenario == 'cancel_denied' else 3)
                        self.assertEqual(sleep.call_count, 0 if scenario == 'cancel_denied' else 2)


    def test_receipt_base_is_pinned_for_both_pools_and_retry(self):
        self.assertEqual(s.RECEIPT_BASE_REFS, {"main", RELEASE_BASE})
        for attempt, pool in ((1, "cs"), (2, "idc")):
            t = dict(self.t, attempt=attempt, group=attempt-1, title_version=2)
            grant = dict(child=51, ticket=t, pool=pool, nonce="test-nonce")
            child = dict(id=51, repository={"full_name": s.REPO}, run_attempt=1,
                         path=".github/workflows/"+s.WORKER, event="workflow_dispatch",
                         head_branch=s.WORKER_REF, head_sha=t["revision"], display_title=s.short_title(t))
            marker = dict(run_id=51, run_attempt=1, status="completed", conclusion="skipped",
                          name=s.title(t, pool)+s.IDENTITY_SUFFIX)
            env = dict(TICKET=json.dumps(t), GROUP=str(t["group"]), HEAD=self.sha, SOURCE=self.src,
                       POOL=pool, GITHUB_RUN_ATTEMPT="1", GITHUB_RUN_ID="51")
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
                with self.subTest(attempt=attempt, pool=pool, schema=schema, authorized=authorized,
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
        for name in (s.GRANT_STEP, s.WAIT_STEP, s.REVOKE_STEP):
            self.assertEqual(selector.count(f'name: {name}\n'), 1)
        self.assertLess(selector.index('coverage_standby.py choose'), selector.index('coverage_standby.py wait'))
        self.assertIn("always() && steps.select.outputs.child != '' && steps.wait.outcome != 'success'", selector)
        self.assertIn('coverage_standby.py revoke', selector)
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

    def test_job_details_are_rate_limited_but_child_result_is_polled_each_time(self):
        interval = s.JOB_DETAIL_INTERVAL
        times = [0, interval-1, interval, interval+1]
        finish = dict(status="completed", run_attempt=1, conclusion="success")
        with patch.dict(os.environ, dict(CHILD="123", GROUP="1", POOL="cs")), \
             patch.object(s, "run", side_effect=[dict(status="in_progress")]*len(times)+[finish]) as runs, \
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

    def test_dispatch_requests_run_id_and_checks_the_returned_worker(self):
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
             patch.object(s, "one", return_value={"total_count": 1, "jobs": [self.marker]}) as lookup, \
             patch.object(s, "run", return_value=self.meta):
            self.assertEqual(s.dispatch(self.ticket, "cs", "b"*40), 51)
            post.assert_called_once()
            self.assertIs(post.call_args.args[1]["return_run_details"], True)
            lookup.assert_called_once_with("/actions/runs/51/attempts/1/jobs?per_page=100")

    def test_dispatch_retries_a_queued_marker_for_the_returned_id(self):
        queued = dict(self.marker, status="queued", conclusion=None)
        with patch.object(s, "parent_meta"), patch.object(s, "api", return_value=({"workflow_run_id": 51}, 0)) as post, \
             patch.object(s, "one", side_effect=[{"total_count": 1, "jobs": [m]} for m in (queued, self.marker)]), \
             patch.object(s, "run", return_value=self.meta), patch.object(s.time, "sleep") as sleep:
            self.assertEqual(s.dispatch(self.ticket, "cs", "b"*40), 51)
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
        for state in ("missing", "queued", "vanished", "duplicate"):
            def read(path):
                if "/actions/workflows/" in path:
                    return {"workflow_runs": [other, self.meta]}
                if "/52/" in path:
                    if state == "vanished":
                        raise urllib.error.HTTPError(path, 404, "not found", None, None)
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
                        s.dispatch(self.ticket, "cs", "b"*40)
                    metadata.assert_not_called()
                else:
                    self.assertEqual(s.dispatch(self.ticket, "cs", "b"*40), 51)
                    metadata.assert_called_once_with(51)
                post.assert_called_once()
                sleep.assert_not_called()
