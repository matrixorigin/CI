"""Narrow, new MTY CPU-generation producer pools; never a PR traffic route.

Pure templates and admission checks only. No cluster writes, count cap, cache
publication or compatibility alias. Resource shortages mean wait, not resize.
"""
import copy

import incremental_cache as c
import node_cache_demand_fleet as demand
import node_cache_profile_append_control as profiles
import node_cache_publish_control as pub

WORK_GIB = 80
LISTENER_NODE = 'mo-standard-0'
LISTENER_RESOURCES = {'requests': {'cpu': '100m', 'memory': '128Mi'},
                      'limits': {'cpu': '500m', 'memory': '256Mi'}}


def effective_budget(spec):
    """Per-resource peak limits/requests, including ordered restartable init.

    A terminating init does not overlap the application, but every preceding
    restartPolicy=Always init does. Overhead is charged to every peak. Existing
    workload accounting stays conservatively unchanged in admission().
    """
    def value(container):
        resources = container.get('resources', {})
        return [max(pub.quantity(resources.get('requests', {}).get(k, 0)),
                    pub.quantity(resources.get('limits', {}).get(k, 0)))
                for k in ('cpu', 'memory')]
    sidecars = [0., 0.]; peak = [0., 0.]
    for container in spec.get('initContainers', []):
        amount = value(container)
        concurrent = [a+b for a, b in zip(sidecars, amount)]
        peak = [max(a, b) for a, b in zip(peak, concurrent)]
        if container.get('restartPolicy') == 'Always':
            sidecars = concurrent
    app = [sum(value(x)[i] for x in spec['containers'])+sidecars[i] for i in range(2)]
    return [max(a, b)+pub.quantity(spec.get('overhead', {}).get(k, 0))
            for a, b, k in zip(peak, app, ('cpu', 'memory'))]


def pool(policy, binding, original, storage_class):
    node = policy['node']
    c.require(policy['environment'] == 'mty' and node in profiles.PROFILES,
              'only the independently observed missing MTY CPU profiles')
    result = demand.pool(policy, binding, original, storage_class, WORK_GIB)
    number = node[-1]; name = f'cs-mty-profile-n{number}-v1'
    labels = result['metadata']['labels']
    labels['codex.matrixorigin.io/experiment'] = labels['app.kubernetes.io/name'] = name
    result['metadata']['name'] = name
    spec = result['spec']
    spec['runnerScaleSetName'] = name
    spec['runnerScaleSetLabels'] = [f'cs-mty-cpu-producer-n{number}']
    spec['template']['metadata']['labels'] = copy.deepcopy(labels)
    # A new bounded listener must not consume node2's scarce runner memory.
    listener = spec['listenerTemplate']['spec']
    c.require(len(listener.get('containers', [])) == 1 and
              listener['containers'][0]['name'] == 'listener' and
              not listener.get('initContainers'), 'unreviewed listener template')
    listener['containers'][0]['resources'] = copy.deepcopy(LISTENER_RESOURCES)
    listener['nodeSelector'] = {'kubernetes.io/hostname': LISTENER_NODE,
                               'matrixone.cloud/profile': 'cn.cloudsigma'}
    listener['tolerations'] = copy.deepcopy(spec['template']['spec']['tolerations'])
    c.require('minRunners' not in spec and 'maxRunners' not in spec and
              demand.COMMON_ROUTE not in spec['runnerScaleSetLabels'], 'producer route isolation differs')
    return result


def quota(snapshot, pod_specs, claims):
    wanted = {'pods': len(pod_specs), 'count/pods': len(pod_specs),
              'persistentvolumeclaims': claims, 'count/persistentvolumeclaims': claims,
              'requests.storage': claims*WORK_GIB*c.GIB,
              'directpv-min-io-ephemeral.storageclass.storage.k8s.io/persistentvolumeclaims': claims,
              'directpv-min-io-ephemeral.storageclass.storage.k8s.io/requests.storage': claims*WORK_GIB*c.GIB}
    # Namespace quota uses effective per-Pod requests and limits separately.
    for kind in ('requests', 'limits'):
        for i, key in enumerate(('cpu', 'memory')):
            specs = copy.deepcopy(pod_specs)
            for spec in specs:
                for container in spec['containers']+spec.get('initContainers', []):
                    resources = container.get('resources', {})
                    container['resources'] = {kind: resources.get(kind, {})}
            wanted[kind+'.'+key] = sum(effective_budget(s)[i] for s in specs)
    for item in snapshot['quotas']['items']:
        status = item.get('status', {})
        for key, needed in wanted.items():
            hard = status.get('hard', item.get('spec', {}).get('hard', {}))
            if key in hard:
                c.require(pub.quantity(status.get('used', {}).get(key, 0))+needed <= pub.quantity(hard[key]),
                          'producer quota unavailable; wait without changing quota')


def admission(resource, snapshots):
    """Additional full producer+listener budget AFTER fresh identity/activity gates.

    No fake node0 metrics, helper sizing or relaxed reserve. This checks ONE
    explicit compile-only job; it is not demand-fleet concurrency admission.
    """
    spec = resource['spec']; runner = spec['template']['spec']
    node = runner['nodeSelector']['kubernetes.io/hostname']
    c.require(resource['metadata']['name'] == f'cs-mty-profile-n{node[-1]}-v1'
              and node in profiles.PROFILES and spec['runnerScaleSetLabels'] == [f'cs-mty-cpu-producer-n{node[-1]}'],
              'producer resource scope differs')
    c.require(runner['containers'][0]['resources'] ==
              {'requests': {'cpu': '7', 'memory': '12Gi'}, 'limits': {'cpu': '7', 'memory': '14Gi'}},
              'producer resource contract differs')
    listener = spec['listenerTemplate']['spec']
    c.require(listener['nodeSelector']['kubernetes.io/hostname'] == LISTENER_NODE and
              listener['containers'][0]['resources'] == LISTENER_RESOURCES, 'producer listener budget differs')
    proof = {}
    for target, extra in ((node, runner), (LISTENER_NODE, listener)):
        snapshot = snapshots[target]
        cpu, memory = effective_budget(extra)
        for pod in snapshot['pods']['items']:
            if pod['spec'].get('nodeName') == target and pod.get('status', {}).get('phase') not in ('Succeeded', 'Failed'):
                a, b = pub.pod_budget(pod); cpu += a; memory += b
        allocated = next(n for n in snapshot['nodes']['items'] if n['metadata']['name'] == target)['status']['allocatable']
        stat = snapshot['stats']['node']
        new_cpu, new_memory = effective_budget(extra)
        c.require(cpu+2 <= pub.quantity(allocated['cpu']) and memory+4*c.GIB <= pub.quantity(allocated['memory']),
                  'full producer declared resources unavailable; wait')
        c.require(stat['memory']['availableBytes'] >= new_memory+4*c.GIB and
                  stat['fs']['availableBytes'] >= 60*c.GIB,
                  'full producer actual resources unavailable; wait')
        proof[target] = {'combined_cpu_limits': cpu, 'combined_memory_limits_bytes': memory,
                         'additional_peak_cpu': new_cpu, 'additional_peak_memory_bytes': new_memory,
                         'memory_available_bytes': stat['memory']['availableBytes'],
                         'root_free_bytes': stat['fs']['availableBytes'], 'memory_reserve_bytes': 4*c.GIB}
    free = max((d['status'].get('freeCapacity', 0) for d in snapshots[node]['drives']['items']
                if d['metadata'].get('labels', {}).get('directpv.min.io/node') == node
                and d['metadata']['labels'].get('directpv.min.io/tier') == 'ephemeral'
                and d['status'].get('status') == 'Ready'), default=0)
    c.require(free >= (WORK_GIB+20)*c.GIB, 'producer private workspace reservation unavailable; wait')
    quota(snapshots[node], [runner, listener], 1)
    return {'nodes': proof, 'private_workspace_bytes': WORK_GIB*c.GIB,
            'physical_free_bytes': free, 'count_cap_authored': False, 'pr_traffic_enabled': False}


def listener_identity(resource, actual_set, listener, pod):
    """ARC listeners lack a set ownerRef; verify explicit set refs AND scale ID.

    The listener Pod must still be owned exclusively by this exact Listener
    UID, on the reviewed listener node and with the bounded resource template.
    """
    name = resource['metadata']['name']; spec = listener['spec']
    scale_id = actual_set['metadata'].get('annotations', {}).get('runner-scale-set-id')
    c.require(actual_set['metadata']['name'] == name and actual_set['metadata']['namespace'] == 'cicd'
              and actual_set['spec'] == resource['spec'] and actual_set['metadata'].get('uid')
              and str(scale_id).isdigit() and int(scale_id) > 0, 'producer set identity differs')
    c.require(listener['metadata']['namespace'] == 'cicd' and
              spec.get('autoscalingRunnerSetName') == name and
              spec.get('autoscalingRunnerSetNamespace') == 'cicd' and
              str(spec.get('runnerScaleSetId')) == scale_id and
              not listener['metadata'].get('deletionTimestamp'), 'producer Listener references differ')
    for value in (listener, pod):
        labels = value['metadata'].get('labels', {})
        c.require(labels.get('actions.github.com/scale-set-name') == name and
                  labels.get('actions.github.com/scale-set-namespace') == 'cicd' and
                  not value['metadata'].get('deletionTimestamp'), 'producer listener label identity differs')
    owners = pod['metadata'].get('ownerReferences', [])
    c.require(len(owners) == 1 and all(owners[0].get(k) == v for k, v in
              {'apiVersion': 'actions.github.com/v1alpha1', 'kind': 'AutoscalingListener',
               'name': listener['metadata']['name'], 'uid': listener['metadata']['uid'], 'controller': True}.items()),
              'producer listener Pod owner differs')
    c.require(pod['spec'].get('nodeName') == LISTENER_NODE and len(pod['spec']['containers']) == 1 and
              pod['spec']['containers'][0]['name'] == 'listener' and
              pod['spec']['containers'][0]['resources'] == LISTENER_RESOURCES and
              not pod['spec'].get('initContainers') and
              pod['status']['phase'] == 'Running' and
              any(x['type'] == 'Ready' and x['status'] == 'True' for x in pod['status'].get('conditions', [])) and
              not any(x.get('restartCount', 0) for x in pod['status'].get('containerStatuses', [])),
              'producer listener placement/resources/readiness differ')
    return {'set_uid': actual_set['metadata']['uid'], 'listener_uid': listener['metadata']['uid'],
            'listener_pod_uid': pod['metadata']['uid'], 'node': LISTENER_NODE,
            'scale_set_id': int(scale_id), 'pr_route_enabled': False}
