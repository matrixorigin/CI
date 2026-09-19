import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from race_seed_canary import compare, execution, summarize, resource_summary, cgroup_summary
from race_seed_plan import matrix


class EvidenceTests(unittest.TestCase):
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
