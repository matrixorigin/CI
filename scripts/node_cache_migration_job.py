#!/usr/bin/env python3
"""Isolated runner migration entry: read existing audited PVC, stage privately.

No compiler or tests are invoked; no writable shared mount or cluster token.
The returned artifact is a candidate for protected review, not self-published.
"""
import os
from pathlib import Path
import shutil
import tarfile
import time
import traceback

import incremental_cache as c
import migrate_node_cache as m
import node_compile_cache as n


POOLS = {'mty': 'cs-mty-pvc-g0-cpe8k', 'jed': 'cs-jed-pvc-g0-cpe8k',
         'ruh': 'cs-ruh-pvc-g0-rhprep', 'mnl2': 'cs-mnl2-pvc-g0-vlleop'}


def package(candidate, receipt, entry, destination):
    candidate = c.safe_path(candidate); destination = c.safe_path(destination)
    c.require(not destination.exists() and candidate not in destination.parents and
              destination not in candidate.parents, 'migration bundle must be fresh and separate')
    index = candidate/'index.json'
    c.require(c.digest(index) == receipt['index_sha256'] and receipt['compiler_invocations'] == 0 and
              receipt['native_imported'] is False, 'migration receipt differs')
    mapping = c.read_json(index); objects = {}
    for action, record in mapping['actions'].items():
        c.check_record(action, record)
        c.require(record['output_id'] not in objects or objects[record['output_id']] == record['size'], 'object alias')
        objects[record['output_id']] = record['size']
    total = index.stat().st_size+sum(objects.values())
    c.require(total <= 8*c.GIB and shutil.disk_usage(destination.parent).free >= 3*total+20*c.GIB,
              'migration packaging reserve insufficient')
    destination.mkdir(); archive = destination/'delta.tar.gz'
    files = [('index.json', index, index.stat().st_size, receipt['index_sha256'])]
    files += [('objects/'+ident[:2]+'/'+ident, candidate/'objects'/ident[:2]/ident, size, ident)
              for ident, size in sorted(objects.items())]
    started = time.monotonic()
    with archive.open('xb') as output, tarfile.open(fileobj=output, mode='w:gz', compresslevel=1) as tar:
        for name, path, size, digest in files:
            c.require(path.stat().st_size == size and c.digest(path) == digest, 'migration object changed')
            info = tarfile.TarInfo(name); info.size = size; info.mode = 0o444
            with c.safe_open(path) as body: tar.addfile(info, body)
    c.require(archive.stat().st_size <= 8*c.GIB, 'compressed migration exceeds budget')
    with c.safe_open(index) as body:
        c.atomic_file(destination/'index.json', body, index.stat().st_size, receipt['index_sha256'])
    manifest = {'schema': 2, 'compile_only': True, 'runtime': receipt['runtime'],
                'source': entry['source'], 'parent_indexes': [], 'native_manifests': {},
                'index_sha256': receipt['index_sha256'], 'object_count': len(objects),
                'object_bytes': sum(objects.values()), 'unpacked_bytes': total,
                'archive_bytes': archive.stat().st_size, 'archive_sha256': c.digest(archive),
                'migration': receipt, 'pack_seconds': round(time.monotonic()-started, 6)}
    c.atomic_json(destination/'manifest.json', manifest)
    return manifest


def main(progress=lambda stage: None):
    progress('runner_scope')
    env = os.environ['CS_CACHE_ENV']; runner = os.environ['RUNNER_NAME']
    c.require(env in POOLS and runner.startswith(POOLS[env]+'-'), 'unexpected isolated migration runner')
    source = Path(os.environ['GITHUB_WORKSPACE']).resolve(); tmp = Path(os.environ['RUNNER_TEMP']).resolve()
    c.require(shutil.disk_usage(tmp).free >= 60*c.GIB, 'migration startup reserve below 60 GiB')
    entries = c.read_json(Path(__file__).parents[1]/'config/node-cache-migration-catalog.json')['entries']
    selected = [entry for entry in entries if entry['environment'] == env and entry['group'] == '0']
    c.require(len(selected) == 1, 'ambiguous migration entry'); entry = selected[0]
    c.require(n.command(['git', 'rev-parse', 'HEAD'], source) == entry['source'], 'migration source differs')
    shared = Path('/ci-cache'); n.readonly_mount(shared)
    progress('runtime_fingerprint')
    runtime = n.runtime(source, env, 'matrixorigin/matrixone')
    c.atomic_json(tmp/'node-cache-migration-runtime.json', runtime)
    directory = shared/('reviewed-'+str(entry['artifact']['id']))
    candidate = tmp/'node-cache-converted'
    receipt = m.migrate(directory, entry, runtime, candidate, progress=progress)
    progress('private_bundle_packaging')
    manifest = package(candidate, receipt, entry, tmp/'node-cache-migration-bundle')
    print(c.canonical({'environment': env, 'compiler_invocations': 0, 'native_imported': False,
                       'object_count': manifest['object_count'], 'object_bytes': manifest['object_bytes'],
                       'index_sha256': manifest['index_sha256']}).decode())


def execute():
    # Exception messages / commands / URLs can contain credentials. Retain only
    # fixed phase IDs, source locations and exception type, never their content.
    started=time.monotonic(); report={'schema':1,'compiler_invocations':0,'shared_writable':False,'phases':[]}
    def progress(stage):
        report['phase']=stage
        report['phases'].append({'stage':stage,'seconds':round(time.monotonic()-started,6)})
    code=0
    try:
        main(progress);report['success']=True
    except Exception as error:
        code=1;report['success']=False;report['error_type']=type(error).__name__
        report['frames']=[{'file':Path(f.filename).name,'line':f.lineno}
            for f in traceback.extract_tb(error.__traceback__) if Path(f.filename).name in
            ('node_cache_migration_job.py','migrate_node_cache.py','node_compile_cache.py','incremental_cache.py','cs_coverage_cache.py')]
    c.atomic_json(Path(os.environ['RUNNER_TEMP'])/'node-cache-migration-diagnostic.json',report)
    print(c.canonical(report).decode())
    return code


if __name__ == '__main__':
    raise SystemExit(execute())
