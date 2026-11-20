"""Verify an unpublished group-1 candidate in new private staging, not on the PVC.

The reviewer supplies the entry AND its SHA from an independent protected
record (not the producer artifact). This is evidence, never a publish grant.
"""
import argparse
from pathlib import Path

import incremental_cache as c
import mty_n0_prepare_group1 as prepare
import mty_n0_refresh_preflight as pre
import node_cache_transport as transport
import node_compile_cache as n


def verify(entry_path, expected_sha, work, receive=transport.receive):
    c.require(c.HEX.fullmatch(expected_sha) and c.digest(entry_path) == expected_sha,
              'review record differs from protected pin')
    entry = c.read_json(entry_path)
    producer = entry['producer']
    c.require(producer['repository'] == 'matrixorigin/matrixone' and
              producer['event'] in ('push', 'workflow_dispatch') and
              producer['run_attempt'] == 1 and entry['source'] == pre.SOURCE and
              entry['compile_only'] is True and entry['bindings'] == [],
              'not the trusted baseline or not an unpublished candidate')
    c.require(c.digest(pre.AUTH) == pre.AUTH_SHA, 'base authorization changed')
    catalog = c.read_json(pre.AUTH)['base_catalog']
    binding, = [b for b in catalog['nodes'] if (b['environment'], b['node']) == ('mty', pre.NODE)]
    parents = [e['runtime'] for e in n.catalog_for_node(catalog, binding)['entries']]
    pre.check_runtime(entry['runtime'], parents)
    c.require(len(entry['native_manifests']) == 1, 'expected exactly one native build')
    work = c.safe_path(work)
    work.mkdir(mode=0o700, exist_ok=False)
    # Checks live Actions provenance, archive inventory and the bytes of every
    # indexed Go and native object. No cache publication API is called.
    index = receive(entry, work/'transport', work/'checked')
    manifest = c.read_json(work/'transport'/'manifest.json')
    prepare.check_scope(manifest['contract'])
    c.require(manifest['contract']['flags'] == n.legacy.FLAGS and
              manifest['source'] == pre.SOURCE and manifest['parent_indexes'] == [] and
              c.digest(index) == entry['index_sha256'] and manifest['object_count'] > 0,
              'unexpected compile contract, source, parents or index')
    return {'stage':'candidate-bytes-only', 'producer_run':producer['id'],
            'artifact_id':entry['artifact']['id'], 'entry_sha256':expected_sha,
            'index_sha256':entry['index_sha256'], 'objects':manifest['object_count'],
            'target_node':binding['node'], 'shared_cache_writes':0,
            'pvc_verified':False, 'publication_authorized':False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reviewed-entry', type=Path, required=True)
    p.add_argument('--protected-entry-sha256', required=True)
    p.add_argument('--fresh-private-directory', type=Path, required=True)
    args = p.parse_args()
    print(c.canonical(verify(args.reviewed_entry, args.protected_entry_sha256,
                             args.fresh_private_directory)).decode())
