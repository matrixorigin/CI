import copy
import errno
import io
import json
import os
import runpy
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest import mock
import race_seed_canary as canary

from race_seed_canary import compare, execution, summarize, resource_summary, cgroup_summary
from race_seed_plan import matrix


class EvidenceTests(unittest.TestCase):
    def test_plan_checks_trusted_source_and_exact_harness_without_squash_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            env = dict(SOURCE_SHA='1' * 40, CI_SHA='2' * 40,
                       SEED_IMAGE='matrixorigin/matrixone@sha256:' + '3' * 64,
                       REPETITIONS='1', GITHUB_OUTPUT=str(output))
            script = str(Path(__file__).with_name('race_seed_plan.py'))
            with mock.patch.dict(os.environ, env), \
                    mock.patch('subprocess.check_output', side_effect=['ahead', '2' * 40]) as command:
                runpy.run_path(script, run_name='__main__')
                self.assertEqual(len(json.loads(output.read_text().split('=', 1)[1])['include']), 4)
                self.assertEqual(command.call_count, 2)
            with mock.patch.dict(os.environ, env), \
                    mock.patch('subprocess.check_output', side_effect=['ahead', '4' * 40]):
                with self.assertRaisesRegex(ValueError, 'checkout identity'):
                    runpy.run_path(script, run_name='__main__')
            with mock.patch.dict(os.environ, env), \
                    mock.patch('subprocess.check_output', return_value='diverged'):
                with self.assertRaisesRegex(ValueError, 'official main'):
                    runpy.run_path(script, run_name='__main__')

    def test_audited_runner_preparation_preserves_preexisting_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, cache, modules = (root / n for n in ('source', 'cache', 'modules'))
            source.mkdir()
            environment = dict(RUNNER_ENVIRONMENT='self-hosted',
                               RUNNER_NAME='amd64-mo-shanghai-8c16g-abc-runner-def',
                               GITHUB_WORKSPACE=str(source), CANARY_SOURCE_SHA='s', CANARY_CI_SHA='c')
            def command(args, cwd=None):
                return ('s' if cwd == source else 'c') if args[0] == 'git' else ''
            with mock.patch.multiple(canary, SOURCE=source, CACHE=cache, MODULES=modules), \
                    mock.patch.object(canary, 'command', side_effect=command), \
                    mock.patch.dict(os.environ, environment):
                self.assertEqual(canary.prepare(None)['cache_state'], 'cold')
                (cache / 'existing').write_text('retain')
                with self.assertRaisesRegex(ValueError, 'preexisting'):
                    canary.prepare(None)
                self.assertEqual((cache / 'existing').read_text(), 'retain')
                (cache / 'existing').unlink()
                snapshot = root / 'snapshot'
                snapshot.mkdir()
                archive = snapshot / 'cache.tar'
                with tarfile.open(archive, 'w') as stream:
                    for name in ('go-build/cache-hit', 'mod/example.test/m.go'):
                        member = tarfile.TarInfo(name)
                        member.size = 4
                        stream.addfile(member, io.BytesIO(b'data'))
                (snapshot / 'snapshot.json').write_text(json.dumps({
                    'sha256': canary.sha256(archive), 'identity': {'fixture': 'same'}}))
                with mock.patch.object(canary, 'fingerprint', return_value={'fixture': 'same'}), \
                        mock.patch('shutil.os.rename', side_effect=OSError(errno.EXDEV, 'cross-device')):
                    self.assertEqual(canary.prepare(snapshot)['cache_state'], 'warm')
                self.assertEqual((cache / 'cache-hit').read_bytes(), b'data')
                self.assertEqual((modules / 'example.test/m.go').read_bytes(), b'data')
                self.assertFalse((cache / 'go-build').exists())
                with mock.patch.dict(os.environ, RUNNER_NAME='unknown-runner'):
                    with self.assertRaisesRegex(ValueError, 'audited'):
                        canary.prepare(None)

    def test_cgroup_metrics_use_quota_not_host_cpu_count(self):
        rows = [dict(monotonic=i * 10, cgroup_cpu=f'usage_usec {i * 40000000}\nthrottled_usec {i * 1000000}',
                     cgroup_cpu_limit='800000 100000', cgroup_memory_limit='17179869184',
                     cgroup_memory=str(100 + i * 50), cgroup_memory_events=f'oom {i}\noom_kill 0')
                for i in range(2)]
        result = cgroup_summary(rows)
        self.assertEqual(result['cpu_quota_utilization_percent'], 50)
        self.assertEqual(result['cpu_seconds'], 40)
        self.assertEqual(result['cpu_throttled_seconds'], 1)
        self.assertEqual(result['sampled_peak_memory_bytes'], 150)
        self.assertEqual(result['oom_events'], 1)
        self.assertFalse(cgroup_summary([{}, {}])['available'])
        unlimited = [dict(r, cgroup_cpu_limit='max 100000', cgroup_memory_limit='max') for r in rows]
        self.assertIsNone(cgroup_summary(unlimited)['cpu_quota_utilization_percent'])
        for key, value in [('cgroup_cpu_limit', '400000 100000'),
                           ('monotonic', 0), ('cgroup_cpu', 'usage_usec -1\nthrottled_usec 0')]:
            changed = copy.deepcopy(rows)
            changed[1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                cgroup_summary(changed)

    def read(self, events):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'raw.json'
            path.write_text('\n'.join(json.dumps(e) for e in events))
            return execution(path)

    def events(self):
        return [{'Package': 'p', 'Action': 'start'}] + [
            {'Package': 'p', 'Test': 'TestX', 'Action': action} for action in ('run', 'pass')] + [
            {'Package': 'p', 'Action': 'pass'}]

    def test_execution_requires_real_nonempty_complete_tests(self):
        for events in ([], [{'Package': 'p', 'Action': 'pass'}], self.events()[:1],
                       self.events()[:-1]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.read(events)
        self.assertEqual(self.read(self.events())['test_runs'], 1)

    def test_failed_or_truncated_output_is_not_success(self):
        events = self.events()
        events[2]['Action'] = 'fail'
        with self.assertRaises(ValueError):
            self.read(events)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'raw.json'
            path.write_text('{"Action":')
            with self.assertRaises(ValueError):
                execution(path)

    def test_execution_multiset_detects_test_substitution_and_duplicates(self):
        a = self.read(self.events())
        replacement = self.events()
        replacement[1]['Test'] = replacement[2]['Test'] = 'TestY'
        self.assertNotEqual(a['outcome_sha256'], self.read(replacement)['outcome_sha256'])
        self.assertEqual(self.read(self.events() * 2)['test_runs'], 2)

    def test_missing_second_package_terminal_is_rejected(self):
        second = [dict(e, Package='q') for e in self.events()[:-1]]
        with self.assertRaises(ValueError):
            self.read(self.events() + second)

    def test_pair_rejects_each_mismatched_contract(self):
        a = dict(valid=True, seed_enabled=False, identity={'sha': 'same'},
                 initial={'snapshot_sha256': 'same'}, initial_images=[],
                 execution=self.read(self.events()), total_seconds=10)
        b = dict(copy.deepcopy(a), seed_enabled=True, total_seconds=8)
        self.assertEqual(compare(a, b)['saved_seconds'], 2)
        for key in ('identity', 'initial', 'initial_images', 'execution', 'valid', 'seed_enabled'):
            changed = copy.deepcopy(b)
            changed[key] = False if key in ('valid', 'seed_enabled') else 'different'
            with self.subTest(key=key), self.assertRaises(ValueError):
                compare(a, changed)

    def test_matrix_has_both_states_and_alternates_order(self):
        rows = matrix(3)['include']
        self.assertEqual(len(rows), 12)
        self.assertEqual([r['name'] for r in rows[:6]], [
            'race-cold-1-A', 'race-cold-1-B', 'race-warm-1-A', 'race-warm-1-B',
            'race-cold-2-B', 'race-cold-2-A'])
        with self.assertRaises(ValueError):
            matrix(2)

    def test_downloaded_artifact_layout_and_missing_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for state in ('cold', 'warm'):
                for arm in 'AB':
                    target = root / f'race-{state}-1-{arm}'
                    target.mkdir()
                    result = dict(valid=True, seed_enabled=arm == 'B', identity={},
                                  initial={'cache_state': state}, initial_images=[],
                                  execution=self.read(self.events()), total_seconds=10)
                    (target / 'result.json').write_text(json.dumps(result))
            with mock.patch.dict(os.environ, CANARY_REPETITIONS='1',
                                 GITHUB_STEP_SUMMARY=str(root / 'summary.md')):
                summarize(root)
                self.assertEqual(len(json.loads((root / 'comparison.json').read_text())), 2)
                (root / 'race-warm-1-B' / 'result.json').unlink()
                with self.assertRaises(ValueError):
                    summarize(root)

    def test_host_cpu_includes_non_child_work_and_disk_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'samples.jsonl'
            rows = [dict(cpu='cpu 0 0 0 100 0 0 0 0\n', memory='MemAvailable: 100 kB\n', disk_free=100),
                    dict(cpu='cpu 50 0 0 150 0 0 0 0\n', memory='MemAvailable: 50 kB\n', disk_free=20)]
            path.write_text('\n'.join(map(json.dumps, rows)))
            result = resource_summary(path)
            self.assertEqual(result['host_cpu_busy_percent'], 50)
            self.assertEqual(result['host_cpu_peak_interval_percent'], 50)
            self.assertEqual(result['min_disk_free_bytes'], 20)


if __name__ == '__main__':
    unittest.main()
