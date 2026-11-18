"""Compile-only missing CPU profile, never tests or a shared cache writer.

This is a dedicated one-time producer. Ordinary PR consumers do not invoke it.
Its artifact is untrusted until an independent protected publication review.
"""
import argparse
import os
from pathlib import Path

import incremental_cache as c
import node_compile_cache as cache
import node_cache_candidate as candidate
import node_cache_cpu_inventory as cpus

SOURCE = 'dbc7ca14ca8b01cfe6d9e9a151619ea4b5ba14ca'
PROFILES = {'mo-standard-1': '36928e1c2d96ed3c0a80a3f3b29eaeb068634bd5cf0d6ec102767901bd134499',
            'mo-standard-2': '8c424b925b5cb7938a3bad9c724b821753b2ffecea631768e5345579c90dab7e'}


def preflight(state, catalog, node, tools, revision):
    current = state['config']['runtime']
    c.require(node in PROFILES and os.environ['CS_CACHE_NODE'] == os.environ['CS_CACHE_EXPECTED_NODE'] == node
              and os.environ['CS_CACHE_ENV'] == current['environment'] == 'mty'
              and revision == SOURCE and current['repository'] == 'matrixorigin/matrixone',
              'CPU producer source/node scope differs')
    c.require(cpus.checked_cpu(current['cpu']) == PROFILES[node], 'CPU producer actual profile differs')
    c.require(not state['entries'] and not state['config']['indexes'], 'CPU profile already exists; reconcile')
    c.require(tools['persistent_tools'] is True and tools['external_tool_downloads'] == 0,
              'CPU producer must restore persistent local tools')
    binding = cache.verify_binding(catalog, Path(state['config']['shared']), 'mty', node,
        {k: os.environ['CS_CACHE_'+k.upper()] for k in ('node_uid', 'pvc_uid', 'pv_uid')})
    c.require(tools['binding'] == binding, 'CPU producer tools/compile volume differs')
    parents = cache.catalog_for_node(catalog, binding)['entries']
    c.require(parents and all({k: v for k, v in e['runtime'].items() if k != 'cpu'} ==
                             {k: v for k, v in current.items() if k != 'cpu'} for e in parents),
              'CPU producer changed non-CPU ABI/tools/paths')
    return {'source': revision, 'node': node, 'cpu_sha256': PROFILES[node], 'binding': binding,
            'non_cpu_runtime_unchanged': True, 'external_tool_downloads': 0,
            'shared_cache_writes': 0, 'ut_execution': False, 'catalog_activated': False}


def freeze(state_path, full, cover, destination):
    contract = candidate.scope(full, full, cover)
    c.require(len(contract['full_packages']) == 505 and len(contract['coverpkg']) == 484,
              'fixed-source CPU producer full compile scope differs')
    # Native is compiled privately on this exact CPU, never re-labelled.
    c.require(c.read_json(state_path.parent/'native-result.json')['native_hit'] is False,
              'missing CPU producer must emit its own native provenance')
    result = candidate.freeze(state_path, contract, destination)
    c.require(result['compile_only'] and result['source'] == SOURCE and
              cpus.checked_cpu(result['runtime']['cpu']) == PROFILES[os.environ['CS_CACHE_NODE']] and
              result['native_manifests'] and len(result['compile_seconds']) == 17,
              'CPU producer bundle completeness differs')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('preflight', 'freeze'))
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--full', type=Path)
    parser.add_argument('--cover', type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    try:
        if args.phase == 'preflight':
            result = preflight(c.read_json(args.state), c.read_json(args.catalog), os.environ['CS_CACHE_NODE'],
                c.read_json(Path(os.environ['RUNNER_TEMP'])/'persistent-toolchain.json'),
                cache.command(['git', 'rev-parse', 'HEAD'], Path(os.environ['GITHUB_WORKSPACE'])))
            c.atomic_json(Path(os.environ['RUNNER_TEMP'])/'cpu-producer-preflight.json', result)
        else:
            freeze(args.state, args.full, args.cover, args.destination)
    except Exception as error:
        c.record_failure(args.state.parent, error)
        print('CPU compile-only producer rejected; no cache publication.', file=__import__('sys').stderr)
        raise SystemExit(1)
