import copy
import unittest

import node_cache_cpu_producer_fleet as p
import test_node_cache_demand_plan as plans
import test_node_cache_workspace_probe_control as probes


class ProducerFleet(unittest.TestCase):
    def fixture(self, node='mo-standard-1'):
        catalog, policy, original, sc = plans.AllNodePlan().fixture()
        original['spec']['listenerTemplate'] = {'spec': {'containers': [{'name': 'listener'}]}}
        binding = next(b for b in catalog['nodes'] if b['environment'] == 'mty' and b['node'] == node)
        policy.update(node=node, name=f'cs-mty-incremental-n{node[-1]}-v1')
        resource = p.pool(policy, binding, original, sc)
        _, snap = probes.ProbeChecks().snapshot()
        snapshots = {}
        for target in ('mo-standard-0', node):
            local = copy.deepcopy(snap)
            local['nodes']['items'][0]['metadata']['name'] = target
            local['nodes']['items'][0]['status']['allocatable'] = {'cpu': '19.4', 'memory': '18.679Gi'}
            local['stats']['node']['memory']['availableBytes'] = 18.1*p.c.GIB
            local['drives']['items'][0]['metadata']['labels']['directpv.min.io/node'] = target
            snapshots[target] = local
        return policy, binding, original, sc, resource, snapshots

    def test_only_new_profile_routes_same_runner_resources_and_no_count_cap(self):
        for node in p.profiles.PROFILES:
            policy, binding, original, sc, resource, _ = self.fixture(node)
            before = copy.deepcopy((policy, binding, original, sc))
            result = p.pool(policy, binding, original, sc)
            self.assertEqual(result, resource)
            self.assertEqual((policy, binding, original, sc), before)
            spec = resource['spec']; runner = spec['template']['spec']
            self.assertNotIn('minRunners', spec); self.assertNotIn('maxRunners', spec)
            self.assertEqual(spec['runnerScaleSetLabels'], ['cs-mty-cpu-producer-n'+node[-1]])
            self.assertNotIn(p.demand.COMMON_ROUTE, spec['runnerScaleSetLabels'])
            self.assertEqual(p.effective_budget(runner), [7., 14*p.c.GIB])
            self.assertTrue(runner['volumes'][1]['persistentVolumeClaim']['readOnly'])
            self.assertTrue(runner['containers'][0]['volumeMounts'][1]['readOnly'])
            self.assertEqual(runner['volumes'][0]['ephemeral']['volumeClaimTemplate']['spec']['resources']['requests']['storage'], '80Gi')

    def test_listener_scope_and_unreviewed_cpu_refused(self):
        policy, binding, original, sc, _, _ = self.fixture()
        for fault in ('env', 'node', 'listener'):
            a, b, old = copy.deepcopy((policy, binding, original))
            if fault == 'env': a['environment'] = 'ruh'
            if fault == 'node': a['node'] = 'mo-standard-0'
            if fault == 'listener': old['spec']['listenerTemplate']['spec']['containers'].append({'name': 'other'})
            with self.subTest(fault=fault), self.assertRaises(ValueError): p.pool(a, b, old, sc)

    def test_ordered_restartable_init_peak_and_overhead(self):
        def container(memory, restart=None):
            return {'resources': {'limits': {'memory': str(memory)+'Gi', 'cpu': str(memory)}},
                    **({'restartPolicy': restart} if restart else {})}
        spec = {'containers': [container(7)], 'initContainers': [container(12), container(2, 'Always'),
                container(3), container(4, 'Always'), container(8)], 'overhead': {'memory': '128Mi', 'cpu': '100m'}}
        self.assertEqual(p.effective_budget(spec), [14.1, 14*p.c.GIB+128*1024**2])

    def test_actual_node2_shortage_queues_without_reducing_four_gib_reserve(self):
        _, _, _, _, resource, snaps = self.fixture('mo-standard-2')
        proof = p.admission(resource, snaps)
        self.assertEqual(proof['nodes']['mo-standard-2']['memory_reserve_bytes'], 4*p.c.GIB)
        snaps['mo-standard-2']['stats']['node']['memory']['availableBytes'] = 17.75158*p.c.GIB
        with self.assertRaisesRegex(ValueError, 'actual resources unavailable'): p.admission(resource, snaps)

    def test_declared_existing_workloads_listener_root_space_and_quota(self):
        for fault in ('declared', 'listener', 'root', 'space', 'quota'):
            _, _, _, _, resource, snaps = self.fixture()
            source = snaps['mo-standard-1']
            if fault == 'declared':
                source['pods']['items'].append({'metadata': {'uid': 'other'},
                    'spec': {'nodeName': 'mo-standard-1', 'containers': [{'resources': {'limits': {'memory': '1Gi'}}}]},
                    'status': {'phase': 'Running'}})
            if fault == 'listener': snaps['mo-standard-0']['stats']['node']['memory']['availableBytes'] = 4*p.c.GIB
            if fault == 'root': source['stats']['node']['fs']['availableBytes'] = 59*p.c.GIB
            if fault == 'space': source['drives']['items'][0]['status']['freeCapacity'] = 99*p.c.GIB
            if fault == 'quota': source['quotas']['items'] = [{'status': {'hard': {'limits.memory': '14Gi'}, 'used': {}}}]
            with self.subTest(fault=fault), self.assertRaises(ValueError): p.admission(resource, snaps)

    def test_request_quota_uses_peak_init_not_sum_of_nonconcurrent_init(self):
        _, _, _, _, resource, snaps = self.fixture()
        snaps['mo-standard-1']['quotas']['items'] = [{'status': {'hard': {'requests.memory': '12.125Gi',
            'limits.memory': '14.25Gi', 'requests.cpu': '7.1', 'limits.cpu': '7.5', 'pods': '2', 'requests.storage': '80Gi'}, 'used': {}}}]
        p.admission(resource, snaps)

    def listener(self):
        _, _, _, _, resource, _ = self.fixture()
        name = resource['metadata']['name']
        actual = copy.deepcopy(resource)
        actual['metadata'].update(uid='set-uid', annotations={'runner-scale-set-id': '101'})
        labels = {'actions.github.com/scale-set-name': name, 'actions.github.com/scale-set-namespace': 'cicd'}
        listener = {'metadata': {'name': 'new-listener', 'namespace': 'cicd', 'uid': 'listener-uid', 'labels': labels},
            'spec': {'autoscalingRunnerSetName': name, 'autoscalingRunnerSetNamespace': 'cicd', 'runnerScaleSetId': 101}}
        pod = {'metadata': {'name': 'new-listener', 'namespace': 'cicd', 'uid': 'pod-uid', 'labels': labels,
            'ownerReferences': [{'apiVersion': 'actions.github.com/v1alpha1', 'kind': 'AutoscalingListener',
                                'name': 'new-listener', 'uid': 'listener-uid', 'controller': True}]},
            'spec': {'nodeName': p.LISTENER_NODE, 'containers': [{'name': 'listener', 'resources': copy.deepcopy(p.LISTENER_RESOURCES)}]},
            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}], 'containerStatuses': [{'restartCount': 0}]}}
        return resource, actual, listener, pod

    def test_listener_without_set_owner_requires_explicit_refs_id_and_pod_owner(self):
        args = self.listener(); proof = p.listener_identity(*args)
        self.assertEqual(proof['listener_pod_uid'], 'pod-uid')
        for fault in ('set', 'id', 'ref', 'owner', 'route', 'node', 'resource', 'restart'):
            resource, actual, listener, pod = copy.deepcopy(args)
            if fault == 'set': actual['spec']['runnerScaleSetLabels'] = ['ordinary-pr']
            if fault == 'id': listener['spec']['runnerScaleSetId'] = 102
            if fault == 'ref': listener['spec']['autoscalingRunnerSetName'] = 'other'
            if fault == 'owner': pod['metadata']['ownerReferences'][0]['uid'] = 'other'
            if fault == 'route': pod['metadata']['labels']['actions.github.com/scale-set-name'] = 'other'
            if fault == 'node': pod['spec']['nodeName'] = 'mo-standard-2'
            if fault == 'resource': pod['spec']['containers'][0]['resources']['limits']['memory'] = '512Mi'
            if fault == 'restart': pod['status']['containerStatuses'][0]['restartCount'] = 1
            with self.subTest(fault=fault), self.assertRaises(ValueError): p.listener_identity(resource, actual, listener, pod)


if __name__ == '__main__': unittest.main()
