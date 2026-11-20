"""Compile the trusted baseline privately; run no UT, never publish or upload."""
import os
from pathlib import Path

import coverage_two_groups as groups
import incremental_cache as c
import mty_n0_refresh_preflight as probe
import node_cache_candidate as candidate


COLD_SCOPE = '32f7fb7eaee102aac1d4100d8b1c59d98a55e928ef97181f292cd2d575b80c5c'


def check_scope(contract):
    plan = groups.plan(contract['full_packages'], 2)
    c.require(plan['scope_sha256'] == COLD_SCOPE and len(plan['groups'][1]) == 250
              and contract['packages'] == plan['groups'][1] and len(contract['coverpkg']) == 476,
              'scope differs from cold group 1')


def main():
    evidence = probe.inspect()
    tmp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    state = tmp/'cs-node-cache/state.json'
    cfg = c.read_json(state)['config']
    c.require(Path(cfg['local']).resolve() == tmp/'cs-node-cache/objects-private',
              'fresh job-private object store required')
    result = c.read_json(state.parent/'native-result.json')
    c.require(result['native_hit'] is False, 'expected private native build')
    contract = candidate.scope(tmp/'coverage-group.txt', tmp/'coverage-full-scope.txt',
                               tmp/'coverage-instrumented-scope.txt')
    check_scope(contract)
    bundle = candidate.freeze(state, contract, tmp/'unpublished-mty-n0-candidate')
    c.require(bundle['compile_only'] is True and bundle['source'] == probe.SOURCE and
              set(bundle['native_manifests']) == {evidence['native_key']},
              'candidate does not match the reviewed build request')
    print('Private candidate prepared. No publication or UT result.')


if __name__ == '__main__':
    main()
