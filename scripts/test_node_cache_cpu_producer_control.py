import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import node_cache_cpu_producer_control as control
import test_node_cache_cpu_producer_fleet as fixtures
import test_node_cache_demand_plan as plans


class ProducerControl(unittest.TestCase):
    def exercise(self, fault=None):
        q = control.q
        catalog, policy, _, _ = plans.AllNodePlan().fixture()
        _, _, original, _, resource, snapshots = fixtures.ProducerFleet().fixture()
        snapshots['mo-standard-2'] = copy.deepcopy(snapshots['mo-standard-1'])
        args = fixtures.ProducerFleet().listener()
        writes = []; guards = []
        class Fake(q.Session):
            def snapshot(self, node): return copy.deepcopy(snapshots[node])
            def cache_volume(self, binding): return binding
            def call(self, command, data=None, timeout=45):
                if command[0] == 'create':
                    writes.append(copy.deepcopy(data))
                    if fault == 'unknown': raise subprocess.TimeoutExpired('create', 45)
                    made = copy.deepcopy(args[1])
                    assert made['spec'] == data['spec']
                    return made
                assert command[0] == 'get'
                if command[1] == 'autoscalingrunnersets': return {'items': [copy.deepcopy(original)]}
                if command[1] == 'autoscalingrunnerset':
                    return copy.deepcopy(original if command[2] == original['metadata']['name'] else args[1])
                if command[1] == 'autoscalinglisteners':
                    value = copy.deepcopy(args[2])
                    if fault == 'foreign': value['spec']['runnerScaleSetId'] = 102
                    return {'items': [value]}
                if command[1] == 'pods': return {'items': [copy.deepcopy(args[3])]}
                raise AssertionError(command[:2])
        def admission(policy, snapshot, node, owned=(), baseline=None, *args, **kwargs):
            guards.append((node, list(owned), baseline))
            return {'workloads': {}, 'storage_class_uid': 'sc'}
        with tempfile.TemporaryDirectory() as raw, patch.object(q, 'Session', Fake), \
             patch.object(q, 'authenticate'), patch.object(control.profiles, 'cpu_authority'), \
             patch.object(q, 'admission', side_effect=admission):
            directory = Path(raw).resolve()/'intent'
            if fault:
                with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                    control.run(policy, catalog, {}, {}, 'mo-standard-1', 'private', directory, lambda _: {})
                result = json.loads((directory/'state.json').read_text())
                self.assertFalse(result['complete']); self.assertFalse(result['automatic_retry'])
                self.assertEqual(len(writes), 1)
                if fault == 'unknown': self.assertIn('pending_create', result)
            else:
                result = control.run(policy, catalog, {}, {}, 'mo-standard-1', 'private', directory, lambda _: {})
                self.assertTrue(result['complete']); self.assertFalse(result['workflow_dispatched'])
                self.assertFalse(result['pr_traffic_enabled']); self.assertEqual(len(writes), 1)
                self.assertEqual([x[0] for x in guards], list(q.planner.NODES)*3)
                self.assertTrue(all(x[1] == ['pod-uid'] and x[2] == {} for x in guards[-3:]))
                with self.assertRaises(ValueError):
                    control.run(policy, catalog, {}, {}, 'mo-standard-1', 'private', directory, lambda _: {})
                self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0]['kind'], 'AutoscalingRunnerSet')
            self.assertEqual(writes[0]['metadata']['name'], resource['metadata']['name'])

    def test_single_new_set_old_pool_untouched_exact_listener_postflight(self): self.exercise()
    def test_unknown_create_is_journalled_and_never_retried(self): self.exercise('unknown')
    def test_foreign_listener_fails_without_mutation_or_deletion_retry(self): self.exercise('foreign')


if __name__ == '__main__': unittest.main()
