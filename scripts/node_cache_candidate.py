#!/usr/bin/env python3
"""Freeze only new compiler objects BEFORE tests; never publish from a runner.

Go still evaluates its complete input graph, but only missing actions compile.
The candidate is uploaded only after tests pass and is not trusted merely
because this script created it. A protected publisher reviews the successful
producer, artifact and exact manifest/index hashes separately.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tarfile
import time

import incremental_cache as c
import node_compile_cache as n
import publish_node_cache as p

# go test -c retains linked binaries in its temporary work tree until the
# whole invocation exits, even with -o /dev/null. Bound that lifetime while
# keeping the same private GOCACHEPROG and complete coverpkg for every batch.
COVERAGE_BATCH_PACKAGES = 32


def scope(selected, full, cover):
    lists = [path.read_text().strip().splitlines() for path in (selected, full)]
    lists.append(cover.read_text().strip().split(','))
    for values in lists:
        c.require(values and len(values) == len(set(values)) and len(values) <= 10000 and
                  all(re.fullmatch(r'github\.com/matrixorigin/matrixone/[A-Za-z0-9_./-]+', name)
                      and '..' not in name.split('/') for name in values), 'invalid compilation scope')
    c.require(set(lists[0]) <= set(lists[1]) and set(lists[2]) <= set(lists[1]), 'inconsistent scope')
    # Counts are evidence, not compatibility keys. A new SHA may add packages.
    return {'packages': lists[0], 'full_packages': lists[1], 'coverpkg': lists[2],
            'flags': n.legacy.FLAGS}


def compile_commands(contract):
    common = ['go', 'test', '-mod=readonly', '-count=1', '-short', '-tags', 'matrixone_test', '-p', '7']
    packages = contract['packages']
    return [common + ['-c', '-o', os.devnull, './pkg/fileservice']] + [
        ['go', 'test', *contract['flags'], '-coverpkg='+','.join(contract['coverpkg']),
         '-c', '-o', os.devnull, *packages[start:start+COVERAGE_BATCH_PACKAGES]]
        for start in range(0, len(packages), COVERAGE_BATCH_PACKAGES)]


def run_compile(command, source, env, timeout=1800):
    def space():
        c.require(all(shutil.disk_usage(path).free >= 20*c.GIB
                      for path in (source, Path(env['RUNNER_TEMP']))), 'compile reserve reached')
    space(); deadline = time.monotonic()+timeout
    child = subprocess.Popen(command, cwd=source, env=env, start_new_session=True)
    try:
        while True:
            try:
                code = child.wait(timeout=10)
                c.require(code == 0, 'incremental compile failed'); space(); return
            except subprocess.TimeoutExpired:
                space(); c.require(time.monotonic() < deadline, 'incremental compile deadline')
    finally:
        # Only the process group just created above; no Kubernetes/global kill.
        try: os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired: pass
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        if child.poll() is None: child.wait(timeout=5)


def freeze(state_path, contract, destination, execute=run_compile):
    started = time.monotonic(); state = c.read_json(state_path)
    source = c.safe_path(state['source']); cfg = state['config']; local = c.safe_path(cfg['local'])
    destination = c.safe_path(destination)
    c.require(not destination.exists() and local not in destination.parents and
              destination not in local.parents, 'candidate destination must be isolated and fresh')
    c.require(n.runtime(source, cfg['runtime']['environment'], cfg['runtime']['repository']) == cfg['runtime'],
              'runtime changed since selection')
    key, inputs = n.native_key(source, cfg['runtime'])
    c.require(key == state['native_key'] and inputs == state['native_inputs'], 'native inputs changed')
    native = c.read_json(Path(state_path).parent/'native-result.json')
    c.require(n.native_digest(source) == native['native_output_sha256'], 'native output changed')
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(('TEST_', 'CS_S3_', 'MO_CI_S3_', 'AWS_')) or name in (
                'GH_TOKEN', 'GITHUB_TOKEN', 'CS_CACHE_READ_TOKEN'):
            env.pop(name)
    c.require(env.get('GOCACHEPROG') and '-DMO_NATIVE_CACHE_'+native['native_output_sha256']+'=1'
              in env.get('CGO_CFLAGS', ''), 'private incremental cache/native flag missing')
    timings = []; coverage_deadline = None
    for number, command in enumerate(compile_commands(contract)):
        # Splitting an invocation must not multiply its original 30min budget.
        if number == 1:
            coverage_deadline = time.monotonic()+1800
        timeout = 1800 if number == 0 else coverage_deadline-time.monotonic()
        c.require(timeout > 0, 'incremental compile deadline')
        c.atomic_json(Path(state_path).parent/f'compile-phase-{number}-started.json',
                      {'phase': number, 'compile_only': True})
        before = time.monotonic(); execute(command, source, env, timeout=timeout)
        timings.append(round(time.monotonic()-before, 6))
        c.atomic_json(Path(state_path).parent/f'compile-phase-{number}-completed.json',
                      {'phase': number, 'seconds': timings[-1], 'compile_only': True})
    # Cache checks the durable corruption marker even if Go silently fell back.
    # Do not write final acceptance.json here: the full UT hasn't run yet.
    parents = c.Cache(cfg).index
    destination.mkdir()
    index_path = destination/'index.json'
    index = p.export_delta(local, cfg['runtime'], parents, index_path)
    native_manifests = {}
    if not native['native_hit']:
        native_key, mid = p.export_native(source, local, cfg['runtime'])
        c.require(native_key == state['native_key'], 'native key changed while freezing')
        native_manifests[native_key] = mid
    objects = {r['output_id']: r['size'] for r in c.read_json(index_path)['actions'].values()}
    metadata = [('index.json', index_path)]
    for mid in native_manifests.values():
        path = local/'native'/(mid+'.json'); metadata.append(('native/'+mid+'.json', path))
        for r in c.read_json(path)['outputs'].values():
            c.require(r['sha256'] not in objects or objects[r['sha256']] == r['size'], 'object alias')
            objects[r['sha256']] = r['size']
    total = sum(objects.values()) + sum(path.stat().st_size for _, path in metadata)
    c.require(total <= 8*c.GIB and shutil.disk_usage(destination).free >= 3*total+20*c.GIB,
              'incremental candidate export budget')
    files = metadata + [('objects/'+digest[:2]+'/'+digest, local/'objects'/digest[:2]/digest)
                        for digest in sorted(objects)]
    # Explicit inventory only. Never archive the job directory or module cache.
    archive = destination/'delta.tar.gz'
    with archive.open('xb') as output, tarfile.open(fileobj=output, mode='w:gz', compresslevel=1) as tar:
        for name, path in files:
            size = path.stat().st_size
            expected = path.name if path.name in objects else c.digest(path)
            c.require(c.digest(path) == expected and
                      (path.name not in objects or size == objects[path.name]), 'candidate object changed')
            info = tarfile.TarInfo(name); info.size = size; info.mode = 0o444
            with c.safe_open(path) as body: tar.addfile(info, body)
    c.require(archive.stat().st_size <= 8*c.GIB, 'compressed candidate exceeds budget')
    manifest = {'schema': 2, 'compile_only': True, 'runtime': cfg['runtime'],
                'source': n.command(['git', 'rev-parse', 'HEAD'], source),
                'parent_indexes': cfg['indexes'], 'index_sha256': index,
                'native_manifests': native_manifests, 'contract': contract,
                'object_count': len(objects), 'object_bytes': sum(objects.values()),
                'archive_bytes': archive.stat().st_size, 'archive_sha256': c.digest(archive),
                'unpacked_bytes': total, 'compile_seconds': timings,
                'coverage_batch_packages': COVERAGE_BATCH_PACKAGES,
                'freeze_seconds': round(time.monotonic()-started, 6)}
    # Completion marker is last. Partial archives never become valid candidates.
    c.atomic_json(destination/'manifest.json', manifest)
    return manifest


def main():
    def terminate(signum, frame):
        raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('state', 'selected', 'full', 'cover', 'destination'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    try:
        freeze(args.state, scope(args.selected, args.full, args.cover), args.destination)
    except Exception as exc:
        c.record_failure(args.state.parent, exc)
        print('Incremental candidate rejected; no shared cache was modified.', file=__import__('sys').stderr)
        raise SystemExit(1)


if __name__ == '__main__': main()
