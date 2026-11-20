"""One-off, read-only preflight. Produces evidence, never an approval or cache write."""
import json
import os
from pathlib import Path

import incremental_cache as c
import node_compile_cache as n


SOURCE = 'c33ce3ff8e7555c8053c85693e55e93e73d898b7'  # upstream merge base, not fork
PR_HEAD = 'f2d6e753ddc739e63f3cc3f9d13eda06bcd605b4'
AUTH = Path(__file__).resolve().parents[1] / 'config/node-cache-pvc-authorization-mty.json'
AUTH_SHA = '20c9f9b4b6c59c90b0f3ed88aa780b5b3ffc5ae4b6eacb28bef16209a4ee6204'
NODE = 'mo-standard-0'


def check_runtime(current, parents):
    field = 'system_packages_sha256'
    c.require(isinstance(current.get(field), str) and c.HEX.fullmatch(current[field]),
              'missing current package fingerprint')
    c.require(not any(p == current for p in parents), 'compatible generation exists: reconcile first')
    matching = [p for p in parents if set(p) == set(current) and
                {k for k in p if p[k] != current[k]} == {field}]
    c.require(matching, 'not just a package inventory change on this node')
    return sorted({p[field] for p in matching})


def inspect():
    env = os.environ
    c.require(env.get('CS_CACHE_ENV') == 'mty' and
              env.get('CS_CACHE_NODE') == env.get('CS_CACHE_EXPECTED_NODE') == NODE,
              'MTY node0 required')
    c.require(c.digest(AUTH) == AUTH_SHA, 'reviewed reference changed')
    catalog = c.read_json(AUTH)['base_catalog']
    ws = Path(env['GITHUB_WORKSPACE']).resolve(strict=True)
    state = c.read_json(Path(env['RUNNER_TEMP'])/'cs-node-cache/state.json')
    c.require(Path(state['source']).resolve(strict=True) == ws and
              n.command(['git', 'rev-parse', 'HEAD'], ws) == SOURCE, 'wrong upstream checkout')
    shared = Path(state['config']['shared']).resolve(strict=True)
    n.readonly_mount(shared)
    binding = n.verify_binding(catalog, shared, 'mty', NODE,
        {k: env['CS_CACHE_'+k.upper()] for k in ('node_uid', 'pv_uid', 'pvc_uid')})
    c.require(not state['entries'] and not state['config']['indexes'], 'already selected an index')
    current = n.runtime(ws, 'mty', 'matrixorigin/matrixone')
    c.require(current == state['config']['runtime'], 'runtime changed since selection')
    version = n.command(['dpkg-query', '-W', '-f=${Version}', 'linux-libc-dev:amd64'], ws)
    c.require(version == '6.8.0-151.151', 'runner no longer matches cold comparison')
    parents = [e['runtime'] for e in n.catalog_for_node(catalog,binding)['entries']]
    before = check_runtime(current, parents)
    key, inputs = n.native_key(ws, current)
    c.require((key, inputs) == (state['native_key'], state['native_inputs']), 'native inputs changed')
    return dict(source=SOURCE, comparison_pr_head=PR_HEAD, node=NODE,
                binding=binding, previous_package_fingerprints=before,
                current_package_fingerprint=current['system_packages_sha256'],
                linux_libc_dev=version, native_key=key, runtime_sha256=n.hash_value(current),
                shared_writes=0, publication_authorized=False)


if __name__ == '__main__':
    print(json.dumps(inspect(), sort_keys=True))
