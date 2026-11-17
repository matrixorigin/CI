#!/usr/bin/env python3
"""Protected publisher input: authenticate and stage an approved delta bundle.

No source/build execution, no credentials in output, no candidate self-approval.
Kubernetes identity/resource guards remain the caller's responsibility. Staging
does not change a shared PVC; publish_node_cache publishes only reviewed bytes.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile

import incremental_cache as c
import node_compile_cache as n


def stage(directory, reviewed, destination, api=n.legacy.api):
    directory = c.safe_path(directory); destination = c.safe_path(destination)
    c.require(not destination.exists() and directory not in destination.parents and
              destination not in directory.parents, 'fresh disjoint staging path required')
    c.require(n.reviewed_entries({'entries': [reviewed]}, reviewed['runtime'], api) == [reviewed],
              'approved producer unavailable or expired')
    transport = directory/'transport.zip'; artifact = reviewed['artifact']
    c.require(transport.stat().st_size == artifact['size_in_bytes'] and
              'sha256:'+c.digest(transport) == artifact['digest'], 'candidate transport differs')
    manifest_path = directory/'manifest.json'
    c.require(c.digest(manifest_path) == reviewed['manifest_sha256'], 'candidate manifest not approved')
    m = c.read_json(manifest_path)
    c.require(m['schema'] == 2 and m.get('compile_only') is True and
              m['runtime'] == reviewed['runtime'] and m['index_sha256'] == reviewed['index_sha256'] and
              m['native_manifests'] == reviewed.get('native_manifests', {}), 'candidate metadata differs')
    c.require(type(m['unpacked_bytes']) is int and 0 < m['unpacked_bytes'] <= 8*c.GIB and
              type(m['archive_bytes']) is int and 0 < m['archive_bytes'] <= 8*c.GIB, 'candidate budget')
    archive = directory/'delta.tar.gz'
    c.require(archive.stat().st_size == m['archive_bytes'] and c.digest(archive) == m['archive_sha256'],
              'candidate archive differs')
    inventory = {}; metadata = {}; total = 0
    allowed_metadata = {'index.json': m['index_sha256']}
    for mid in m['native_manifests'].values():
        c.require(c.HEX.fullmatch(mid), 'invalid native manifest identity')
        allowed_metadata['native/'+mid+'.json'] = mid
    with c.safe_open(archive) as body, tarfile.open(fileobj=body, mode='r:gz') as tar:
        for member in tar:
            path = n.legacy.safe_relative(member.name)
            c.require(member.isfile() and member.name not in inventory and len(inventory) < 100000,
                      'invalid candidate member')
            c.require(0 <= member.size <= c.MAX_OBJECT, 'candidate member budget')
            total += member.size; c.require(total <= m['unpacked_bytes'], 'candidate expanded budget')
            if member.name in allowed_metadata:
                c.require(member.size <= c.MAX_INDEX, 'candidate metadata too large')
                with tar.extractfile(member) as src: raw = src.read()
                digest = hashlib.sha256(raw).hexdigest()
                c.require(digest == allowed_metadata[member.name], 'candidate metadata checksum')
                # Use the same strict duplicate-key decoder as file reads.
                import json
                def unique(pairs):
                    value = {}
                    for key, item in pairs:
                        c.require(key not in value, 'duplicate candidate JSON key'); value[key] = item
                    return value
                metadata[member.name] = json.loads(raw, object_pairs_hook=unique)
            else:
                c.require(len(path.parts) == 3 and path.parts[0] == 'objects' and
                          c.HEX.fullmatch(path.parts[2]) and path.parts[1] == path.parts[2][:2],
                          'unexpected candidate member')
                digest = hashlib.sha256()
                with tar.extractfile(member) as src:
                    for block in iter(lambda: src.read(1024*1024), b''): digest.update(block)
                c.require(digest.hexdigest() == path.parts[2], 'candidate object checksum')
                digest = digest.hexdigest()
            inventory[member.name] = (member.size, digest)
    c.require(total == m['unpacked_bytes'] and set(metadata) == set(allowed_metadata), 'candidate inventory differs')
    index = metadata['index.json']; objects = {}
    c.require(index['schema'] == 2 and index['runtime'] == reviewed['runtime'] and
              len(index['actions']) <= c.MAX_ACTIONS, 'invalid candidate index')
    def add(ident, size):
        c.check_record('0'*64, {'output_id': ident, 'size': size})
        c.require(ident not in objects or objects[ident] == size, 'object size alias')
        objects[ident] = size
    for action, record in index['actions'].items():
        c.check_record(action, record); add(record['output_id'], record['size'])
    for key, mid in m['native_manifests'].items():
        native = metadata['native/'+mid+'.json']
        c.require(c.HEX.fullmatch(key) and native['key'] == key and native['schema'] == 2 and
                  native['runtime'] == reviewed['runtime'] and native.get('compile_only') is True,
                  'native manifest differs')
        for name, record in native['outputs'].items():
            c.require(n.legacy.native_allowed(name), 'native output not allowlisted')
            add(record['sha256'], record['size'])
    expected = {**{name: inventory[name] for name in allowed_metadata},
                **{'objects/'+ident[:2]+'/'+ident: (size, ident) for ident, size in objects.items()}}
    c.require(inventory == expected and len(objects) == m['object_count'] and
              sum(objects.values()) == m['object_bytes'], 'unreferenced or missing candidate bytes')
    c.require(shutil.disk_usage(destination.parent).free >= total+20*c.GIB, 'staging reserve insufficient')
    destination.mkdir()
    with c.safe_open(archive) as body, tarfile.open(fileobj=body, mode='r:gz') as tar:
        for member in tar:
            size, digest = expected[member.name]
            with tar.extractfile(member) as src:
                c.atomic_file(destination/member.name, src, size, digest)
    c.atomic_json(destination/'validated-bundle.json', {
        'manifest_sha256': reviewed['manifest_sha256'], 'index_sha256': reviewed['index_sha256'],
        'producer_id': reviewed['producer']['id'], 'artifact_id': artifact['id'],
        'new_object_bytes': sum(objects.values()), 'object_count': len(objects)})
    return destination/'index.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'reviewed-entry', 'destination'):
        parser.add_argument('--'+name, type=Path, required=True)
    a = parser.parse_args()
    try: stage(a.bundle, c.read_json(a.reviewed_entry), a.destination)
    except Exception:
        print('Candidate staging rejected; shared cache unchanged.', file=__import__('sys').stderr)
        raise SystemExit(1)


if __name__ == '__main__': main()
