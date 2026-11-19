"""Independent full-byte verification of an already published read-only PVC.

No subprocess, network, compiler, directory creation, deletion or cache writes.
Descriptors derive from the immutable approved catalog, not observed files.
"""
import argparse
from pathlib import Path
import time
import incremental_cache as c
import node_cache_replication as replication
import node_compile_cache as cache
import node_cache_toolchain_store as tools


def verify(shared,catalog,lock,binding):
    shared=c.safe_path(shared);started=time.monotonic()
    cache.readonly_mount(shared)
    c.require(binding in catalog['nodes'],'unapproved persistent volume binding')
    plan=replication.make_plan(shared,catalog,binding,lock)
    expected=replication.expected_files(plan,catalog,binding,lock)
    for name,(size,digest) in expected.items():
        path=c.safe_path(shared/name)
        c.require(path.is_file() and path.stat().st_size==size and c.digest(path)==digest,
            'persistent full-byte revalidation failed; do not authorize or repair automatically')
    entries=replication.entries_for(catalog,binding)
    indexes=[]
    for entry in entries:
        index=c.read_json(shared/'incremental-v2/indexes'/(entry['index_sha256']+'.json'))
        indexes.append({'index_sha256':entry['index_sha256'],'entry_sha256':cache.hash_value(entry),
            'runtime_sha256':cache.hash_value(entry['runtime']),'action_count':len(index['actions']),
            'native_manifests':entry.get('native_manifests',{})})
    tools.verify(shared,lock,binding)
    return {'schema':1,'success':True,'readonly':True,'filesystem_writes':0,
        'compiler_invocations':0,'external_downloads':0,'binding':binding,
        'catalog_sha256':cache.hash_value(catalog),'indexes':indexes,
        'verified_files':len(expected),'verified_bytes':sum(v[0] for v in expected.values()),
        'payload_inventory_sha256':cache.hash_value(expected),'tool_lock_sha256':cache.hash_value(lock),
        'tool_archive_sha256':lock['archive_sha256'],'seconds':round(time.monotonic()-started,6)}


def main():
    parser=argparse.ArgumentParser()
    for name in ('catalog','lock','binding'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--shared',type=Path,default=Path('/ci-cache'))
    args=parser.parse_args()
    value=verify(args.shared,c.read_json(args.catalog.resolve()),c.read_json(args.lock.resolve()),
        c.read_json(args.binding.resolve()))
    print(c.canonical(value).decode())


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(c.canonical({'success':False,'error_type':type(error).__name__,'automatic_retry':False}).decode())
        raise SystemExit(1)
