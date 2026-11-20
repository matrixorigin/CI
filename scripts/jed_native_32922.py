"""Single-use JED candidate contract; never publish to the shared volume."""
import os
from pathlib import Path

import incremental_cache as c
import node_compile_cache as n
from node_cache_native_refresh import pack_native

# Both receipts in matrixorigin/matrixone/actions/runs/44759841661.
SOURCE = 'c33ce3ff8e7555c8053c85693e55e93e73d898b7'
KEY = 'daa5fd9c15d66a8cf28852e376ead1104cf2e3bff8f344be474d5d5b6a5c0331'
RUNTIME = '7b27587670adb3301b3f3d1271b0e586d5738d403eba86c85daa40ee355b078d'
INPUTS = '70fe3b519510a35f7acb3e78a7028f540c40204bf23a9aa1e371ce76a26de055'
TARGETS = {'mo-standard-0': '452d96a5-bc05-4e7d-a1e4-ea1fb39f542a',
           'mo-standard-1': '647e4340-a08e-4efa-9600-965ce9c25a06'}


def validate(revision, runtime, key, inputs, bindings, node):
    c.require(revision == SOURCE and n.hash_value(runtime) == RUNTIME and key == KEY
              and len(inputs) == 196 and n.hash_value(inputs) == INPUTS, 'unapproved native inputs')
    c.require(node == 'mo-standard-0' and bindings == TARGETS, 'unapproved producer or target PVC')


def build():
    tmp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    src = Path(os.environ['GITHUB_WORKSPACE']).resolve(strict=True)
    shared = Path('/ci-cache/incremental-v2')
    n.readonly_mount(shared)
    node = os.environ['CS_CACHE_NODE']
    c.require(os.environ['CS_CACHE_ENV'] == 'jed' and os.environ['CS_CACHE_EXPECTED_NODE'] == node,
              'wrong runner')
    catalog = c.read_json(tmp/'cs-reviewed-persistent-catalog.json')
    rows = [r for r in catalog['nodes'] if r['environment']=='jed' and r['node'] in TARGETS]
    c.require(len(rows)==2, 'target catalog incomplete')
    bindings = {r['node']:r['pvc_uid'] for r in rows}
    binding = n.verify_binding(catalog,shared,'jed',node,
                               {k:os.environ['CS_CACHE_'+k.upper()] for k in ('node_uid','pv_uid','pvc_uid')})
    tools = c.read_json(tmp/'persistent-toolchain.json')
    c.require(tools['persistent_tools'] is True and tools['external_tool_downloads']==0 and
              tools['binding']==binding, 'tool restoration not verified')
    directory = tmp/'cs-node-cache'
    state = c.read_json(directory/'state.json')
    cfg = state['config']
    local = directory/'objects-private'
    c.require(Path(cfg['local'])==local and Path(cfg['shared'])==shared and Path(state['source'])==src,
              'unexpected compilation paths')
    def observe():
        runtime = n.runtime(src,'jed','matrixorigin/matrixone')
        key, inputs = n.native_key(src,runtime)
        validate(n.command(['git','rev-parse','HEAD'],src),runtime,key,inputs,bindings,node)
        c.require(runtime==cfg['runtime'] and key==state['native_key'] and inputs==state['native_inputs'],
                  'configured and measured keys differ')
        return inputs
    observe()
    c.require(n.native_manifest(state) is None, 'native cache already present; not rebuilding')
    n.prepare_native(directory/'state.json')
    inputs = observe()
    report = c.read_json(directory/'native-result.json')
    c.require(report['native_hit'] is False and report['native_output_sha256']==n.native_digest(src),
              'native outputs not freshly built')
    pack_native(src,local,cfg,tmp/'unpublished-jed-native',KEY,inputs,SOURCE,report)


if __name__=='__main__':
    build()
