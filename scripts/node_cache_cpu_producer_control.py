"""Create ONLY a new missing-CPU producer scale set, never an old-pool update.

Exclusive intent precedes the single create; unknown responses are retained for
read-only reconciliation. No dispatch, cache writes or auto-retry in this file.
"""
import copy
from pathlib import Path
import time
import traceback

import incremental_cache as c
import node_cache_cpu_producer_fleet as fleet
import node_cache_profile_append_control as profiles
import node_cache_workspace_probe_control as q


def run(policy, catalog, lock, evidence, node, config, directory, api):
    c.require(not directory.exists(), 'producer intent exists; reconcile, never retry')
    c.require(policy['environment'] == 'mty' and node in profiles.PROFILES, 'producer repair target differs')
    directory.mkdir()
    state = {'complete': False, 'environment': 'mty', 'node': node, 'created': [],
             'automatic_retry': False, 'started_at': q.pub.now().isoformat(),
             'pr_traffic_enabled': False, 'cache_writes': 0, 'workflow_dispatched': False}
    session = q.Session(policy, config, state, directory/'state.json')
    try:
        session.journal()
        q.authenticate(catalog, lock, evidence, 'mty', api)
        profiles.cpu_authority(api)
        original = session.call(['get', 'autoscalingrunnerset', 'cs-mty-large-7c14g', '-n', 'cicd', '-o', 'json'])
        state['original_pool'] = {'uid': original['metadata']['uid'], 'spec_sha256': q.base.replication.sha(c.canonical(original['spec']))}
        binding = next(b for b in catalog['nodes'] if b['environment'] == 'mty' and b['node'] == node)
        snapshots = {n: session.snapshot(n) for n in q.planner.NODES}
        session.baselines = {n: q.admission(policy, s, n) for n, s in snapshots.items()}
        session.cache_volume(binding)
        local = {**copy.deepcopy(policy), 'node': node, 'name': f'cs-mty-incremental-n{node[-1]}-v1'}
        resource = fleet.pool(local, binding, original, snapshots[node]['sc'])
        name = resource['metadata']['name']; route = resource['spec']['runnerScaleSetLabels'][0]
        existing = session.call(['get', 'autoscalingrunnersets', '-n', 'cicd', '-o', 'json'])['items']
        c.require(all(x['metadata']['name'] != name and route not in x['spec'].get('runnerScaleSetLabels', [])
                      and route != x['spec'].get('runnerScaleSetName') for x in existing),
                  'producer name/route exists; reconcile')
        state['desired'] = resource
        state['binding'] = binding
        state['cpu_sha256'] = profiles.PROFILES[node]
        state['preflight'] = session.baselines
        # Re-observe full identities/metrics/workload UIDs immediately before
        # the sole mutation, not an earlier cached readiness report.
        snapshots = {n: session.snapshot(n) for n in q.planner.NODES}
        for n, snapshot in snapshots.items():
            q.admission(policy, snapshot, n, baseline=session.baselines[n]['workloads'])
        state['full_producer_capacity'] = fleet.admission(resource, snapshots)
        session.cache_volume(binding)
        current = session.call(['get', 'autoscalingrunnerset', original['metadata']['name'], '-n', 'cicd', '-o', 'json'])
        c.require(current['metadata']['uid'] == state['original_pool']['uid'] and current['spec'] == original['spec'],
                  'original pool changed before producer create')
        state['pending_create'] = {'kind': 'AutoscalingRunnerSet', 'name': name}
        session.journal()
        made = session.call(['create', '-f', '-', '-o', 'json'], resource)
        c.require(made['kind'] == 'AutoscalingRunnerSet' and made['metadata']['name'] == name and
                  made['metadata']['namespace'] == 'cicd' and made['spec'] == resource['spec'],
                  'producer create response differs; reconcile')
        state['created'] = [{'kind': 'AutoscalingRunnerSet', 'name': name, 'uid': made['metadata']['uid']}]
        state.pop('pending_create'); session.journal()
        # No create retry and no deletion if ARC is slow or a result is unknown.
        deadline = time.monotonic()+600
        while True:
            live = session.call(['get', 'autoscalingrunnerset', name, '-n', 'cicd', '-o', 'json'])
            c.require(live['metadata']['uid'] == made['metadata']['uid'], 'producer set replaced')
            listeners = session.call(['get', 'autoscalinglisteners', '-n', 'cicd', '-o', 'json'])['items']
            matches = [v for v in listeners if v['spec'].get('autoscalingRunnerSetName') == name and
                       v['spec'].get('autoscalingRunnerSetNamespace') == 'cicd']
            c.require(len(matches) <= 1, 'ambiguous producer Listener')
            if matches:
                listener = matches[0]
                pods = session.call(['get', 'pods', '-n', 'cicd', '-o', 'json'])['items']
                matches = [v for v in pods if any(o.get('kind') == 'AutoscalingListener' and
                           o.get('uid') == listener['metadata']['uid'] for o in v['metadata'].get('ownerReferences', []))]
                c.require(len(matches) <= 1, 'ambiguous producer listener Pod')
                if matches and matches[0]['status'].get('phase') == 'Running' and any(
                        x['type'] == 'Ready' and x['status'] == 'True' for x in matches[0]['status'].get('conditions', [])):
                    state['listener'] = fleet.listener_identity(resource, live, listener, matches[0])
                    break
            c.require(time.monotonic() < deadline, 'producer listener Pending; preserve for diagnosis')
            time.sleep(2)
        owned = [state['listener']['listener_pod_uid']]
        state['postflight'] = {}
        for n in q.planner.NODES:
            state['postflight'][n] = q.admission(policy, session.snapshot(n), n, owned,
                session.baselines[n]['workloads'])
        session.cache_volume(binding)
        after = session.call(['get', 'autoscalingrunnerset', original['metadata']['name'], '-n', 'cicd', '-o', 'json'])
        c.require(after['metadata']['uid'] == state['original_pool']['uid'] and after['spec'] == original['spec'],
                  'original pool changed after producer create')
        state.update(complete=True, completed_at=q.pub.now().isoformat())
        session.journal()
        return state
    except Exception as error:
        state.update(error_type=type(error).__name__, failed_at=q.pub.now().isoformat(),
            failure_sites=[{'file': Path(f.filename).name, 'line': f.lineno} for f in traceback.extract_tb(error.__traceback__)
                           if Path(f.filename).name.startswith(('node_cache_', 'incremental_'))])
        session.journal()
        raise
