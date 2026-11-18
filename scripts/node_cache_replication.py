#!/usr/bin/env python3
"""Offline copy of reviewed node-cache objects; never compiles or publishes PR output.

The controller authenticates producer APIs before creating any Pod. Source is
mounted readonly; each destination is a new independent PVC. Every byte is
content-verified and the destination identity/receipt is written last. This
module has no network, subprocess, deletion or credential access.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile

import incremental_cache as c
import node_compile_cache as n
import node_cache_toolchain_store as tools

MAX_BYTES = 24 * c.GIB
PLAN_LIMIT = 96 * 1024**2


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def bound_identity(value):
    required = {'environment', 'node', 'node_uid', 'pvc_uid', 'pv_uid'}
    c.require(set(value) == required and value['environment'] in ('mty', 'jed', 'ruh', 'mnl2'), 'invalid replication identity')
    c.require(value['node'] in ('mo-standard-0', 'mo-standard-1', 'mo-standard-2'), 'invalid replication node')
    import re
    c.require(all(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value[k])
                  for k in ('node_uid', 'pvc_uid', 'pv_uid')), 'invalid replication UID')
    return {**value, 'marker_sha256': sha(c.canonical(value))}


def entries_for(catalog, source):
    c.require(source in catalog['nodes'], 'source binding not reviewed')
    entries = n.catalog_for_node(catalog, source)['entries']
    c.require(entries and len(entries) <= 16 and all(e['runtime']['environment'] == source['environment']
                  and e['compile_only'] is True for e in entries), 'replication source entries differ')
    return entries


def expected_files(plan, catalog, source, lock):
    """Derive the entire allowed payload from pinned indexes, not sender claims."""
    entries = entries_for(catalog, source)
    c.require(plan['schema'] == 1 and plan['source'] == source and plan['tool_lock'] == lock,
              'replication source or tool lock differs')
    tools.validate_lock(lock)
    expected_meta = {}
    objects = {}
    actions_by_runtime = {}
    def add(digest, size):
        c.check_record('0'*64, {'output_id': digest, 'size': size})
        c.require(digest not in objects or objects[digest] == size, 'replication object alias')
        objects[digest] = size
    for entry in entries:
        ident = entry['index_sha256']; name = 'incremental-v2/indexes/' + ident + '.json'
        raw = plan['metadata'][name].encode()
        c.require(sha(raw) == ident, 'replication index differs')
        index = json.loads(raw)
        c.require(index['schema'] == 2 and index['runtime'] == entry['runtime'] and
                  len(index['actions']) <= c.MAX_ACTIONS, 'replication runtime differs')
        merged = actions_by_runtime.setdefault(sha(c.canonical(index['runtime'])), {})
        for action, record in index['actions'].items():
            c.check_record(action, record)
            c.require(action not in merged or merged[action] == record, 'replication action conflict')
            merged[action] = record
            add(record['output_id'], record['size'])
        c.require(len(merged) <= c.MAX_ACTIONS, 'replication action budget')
        expected_meta[name] = (len(raw), ident)
        for key, mid in entry.get('native_manifests', {}).items():
            name = 'incremental-v2/native/' + mid + '.json'; raw = plan['metadata'][name].encode()
            c.require(sha(raw) == mid, 'replication native checksum')
            native = json.loads(raw)
            c.require(native['schema'] == 2 and native['key'] == key and native['runtime'] == entry['runtime']
                      and native['compile_only'] is True, 'replication native scope')
            for path, record in native['outputs'].items():
                c.require(n.legacy.native_allowed(path), 'replication native path')
                add(record['sha256'], record['size'])
            expected_meta[name] = (len(raw), mid)
    c.require(set(plan['metadata']) == set(expected_meta), 'replication metadata inventory')
    payload = {**expected_meta, **{'incremental-v2/objects/' + digest[:2] + '/' + digest: (size, digest)
                                 for digest, size in objects.items()}}
    payload['toolchains-v1/' + lock['archive_sha256'] + '/toolchain.tar.gz'] = (lock['archive_size'], lock['archive_sha256'])
    c.require(sum(size for size, digest in payload.values()) <= MAX_BYTES and len(payload) <= 200000,
              'replication payload budget')
    return payload


def make_plan(shared, catalog, source, lock):
    shared = c.safe_path(shared)
    hints = {k: source[k] for k in ('node_uid', 'pvc_uid', 'pv_uid')}
    c.require(n.verify_binding(catalog, shared/'incremental-v2', source['environment'], source['node'], hints) == source,
              'replication source marker differs')
    tools.verify(shared, lock, source)
    metadata = {}
    for entry in entries_for(catalog, source):
        names = ['incremental-v2/indexes/' + entry['index_sha256'] + '.json']
        names += ['incremental-v2/native/' + mid + '.json' for mid in entry.get('native_manifests', {}).values()]
        for name in names:
            path = c.safe_path(shared/name)
            c.require(path.stat().st_size <= c.MAX_INDEX, 'replication metadata budget')
            metadata[name] = path.read_text()
    plan = {'schema': 1, 'source': source, 'tool_lock': lock, 'metadata': metadata}
    expected_files(plan, catalog, source, lock)
    c.require(len(c.canonical(plan)) <= PLAN_LIMIT, 'replication plan too large')
    return plan


def export(shared, catalog, source, lock, stream):
    plan = make_plan(shared, catalog, source, lock)
    expected = expected_files(plan, catalog, source, lock)
    with tarfile.open(fileobj=stream, mode='w|') as archive:
        raw = c.canonical(plan); info = tarfile.TarInfo('replication.json'); info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
        for name, (size, digest) in sorted(expected.items()):
            path = c.safe_path(shared/name)
            c.require(path.stat().st_size == size and c.digest(path) == digest, 'replication source content corrupt')
            info = tarfile.TarInfo(name); info.size = size; info.mode = 0o444
            with c.safe_open(path) as body: archive.addfile(info, body)


def verify(shared, plan, catalog, source, lock, target):
    shared = c.safe_path(shared); binding = bound_identity(target)
    expected = expected_files(plan, catalog, source, lock)
    c.require(target['environment'] == source['environment'] and target['node'] != source['node'],
              'replication cannot cross environments or overwrite source')
    marker = shared/'incremental-v2/node-cache-identity.json'
    c.require(c.read_json(marker) == target and c.digest(marker) == binding['marker_sha256'], 'target marker differs')
    for name, (size, digest) in expected.items():
        path = c.safe_path(shared/name)
        c.require(path.stat().st_size == size and c.digest(path) == digest, 'replication independent checksum')
    tools.verify(shared, lock, binding)
    return {'success': True, 'independently_verified': True, 'binding': binding,
            'indexes': sorted(e['index_sha256'] for e in entries_for(catalog, source)),
            'files': len(expected), 'bytes': sum(x[0] for x in expected.values()),
            'compiler_invocations': 0, 'external_downloads': 0}


def receive(shared, catalog, source, lock, target, stream):
    shared = c.safe_path(shared); bound_identity(target)
    c.require(target['environment'] == source['environment'] and target['node'] != source['node'], 'wrong replication destination')
    c.require(not (shared/'incremental-v2').exists() and not (shared/'toolchains-v1').exists()
              and not (shared/'.replication-stage').exists(), 'destination is not fresh; reconcile without retry')
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        members = iter(archive)
        header = next(members, None)
        c.require(header is not None and header.name == 'replication.json' and header.isfile()
                  and 0 < header.size <= PLAN_LIMIT, 'replication header differs')
        # Strict duplicate-key JSON parsing is shared with cache metadata reads.
        def unique(pairs):
            value = {}
            for key, item in pairs:
                c.require(key not in value, 'duplicate replication key'); value[key] = item
            return value
        plan = json.load(archive.extractfile(header), object_pairs_hook=unique)
        expected = expected_files(plan, catalog, source, lock)
        c.require(shutil.disk_usage(shared).free >= sum(x[0] for x in expected.values()) + 20*c.GIB,
                  'replication destination reserve')
        stage = shared/'.replication-stage'; stage.mkdir(); seen = set()
        for member in members:
            c.require(member.isfile() and member.name in expected and member.name not in seen, 'unexpected replication member')
            size, digest = expected[member.name]
            c.require(member.size == size, 'replication member size')
            with archive.extractfile(member) as body: c.atomic_file(stage/member.name, body, size, digest)
            seen.add(member.name)
        c.require(seen == set(expected), 'incomplete replication payload')
    binding = bound_identity(target)
    c.atomic_json(stage/'incremental-v2/node-cache-identity.json', target)
    c.atomic_json(stage/'toolchains-v1'/lock['archive_sha256']/'receipt.json',
                  {'schema': 1, 'lock': lock, 'binding': binding})
    verified = verify(stage, plan, catalog, source, lock, target)
    # New-volume publication only. Never overwrite any existing directory.
    for directory in ('incremental-v2', 'toolchains-v1'):
        c.require(not (shared/directory).exists(), 'destination appeared during replication')
        os.rename(stage/directory, shared/directory)
    c.atomic_json(shared/'replication-receipt.json', {**verified, 'source': source, 'complete': True})
    return verified


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('plan', 'export', 'receive', 'verify'))
    for key in ('catalog', 'lock', 'source'):
        parser.add_argument('--'+key, required=True, type=Path)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--target-json', type=json.loads)
    parser.add_argument('--shared', type=Path, default=Path('/ci-cache'))
    args = parser.parse_args()
    # Arguments are trusted files from the controller's readonly ConfigMap.
    # Resolve Kubernetes' projected ..data symlink; cache payload paths never do.
    read = lambda path: c.read_json(path.resolve())
    catalog, source, lock = read(args.catalog), read(args.source), read(args.lock)
    target = args.target_json if args.target_json is not None else read(args.target) if args.target else None
    if args.mode in ('plan','export'): n.readonly_mount(args.shared)
    if args.mode == 'plan': print(json.dumps(make_plan(args.shared, catalog, source, lock)))
    elif args.mode == 'export': export(args.shared, catalog, source, lock, sys.stdout.buffer)
    elif args.mode == 'receive':
        print(json.dumps(receive(args.shared, catalog, source, lock, target, sys.stdin.buffer)))
    else:
        receipt = read(args.shared/'replication-receipt.json')
        plan = make_plan(args.shared, {**catalog, 'nodes': [bound_identity(target)],
            'entries': [{**e, 'bindings': [{k: target[k] for k in ('node_uid','pvc_uid','pv_uid')}]}
                        for e in entries_for(catalog, source)]}, bound_identity(target), lock)
        # Reconstruct immutable payload descriptors with the approved SOURCE
        # identity; target metadata objects remain exactly the original hashes.
        plan['source'] = source
        result = verify(args.shared, plan, catalog, source, lock, target)
        c.require(receipt['complete'] is True and receipt['binding'] == result['binding'], 'replication receipt differs')
        print(json.dumps(result))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'complete': False, 'error_type': type(error).__name__, 'automatic_retry': False}), file=sys.stderr)
        raise SystemExit(1)
