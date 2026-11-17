#!/usr/bin/env python3
"""Run-scoped compile-only seed for the isolated full Coverage comparison."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tarfile
import time

from compile_cache_probe import SOURCE, GIB, sha256, members_safe, regular_files


def coverage_contract(packages, coverpkg, full_packages):
    """Pin the exact selected package order and the production instrumentation."""
    selected = packages.read_text().splitlines()
    full = full_packages.read_text().splitlines()
    instrumented = coverpkg.read_text().strip().split(',')
    for names in (selected, full, instrumented):
        if (not names or len(names) != len(set(names)) or any(
                not re.fullmatch(r'github\.com/matrixorigin/matrixone/[A-Za-z0-9_./-]+', n)
                or '..' in n.split('/') for n in names)):
            raise ValueError('invalid coverage package scope')
    if (len(full) != 505 or len(instrumented) != 484
            or not set(selected) <= set(full) or not set(instrumented) <= set(full)):
        raise ValueError('unexpected fixed-source coverage scope')
    return {'packages': selected, 'full_packages': full, 'coverpkg': instrumented,
            'flags': ['-mod=readonly', '-count=1', '-json', '-short', '-v',
                      '-tags', 'matrixone_test', '-timeout=20m', '-p', '7', '-covermode=set']}


def coverage_compile_command(contract):
    # -c never executes TestMain, init or tests. The null output permits repeated
    # package basenames without retaining hundreds of final test executables.
    return ['go', 'test', *contract['flags'], '-coverpkg=' + ','.join(contract['coverpkg']),
            '-c', '-o', os.devnull, *contract['packages']]


def verify_coverage_contract(manifest, contract):
    if manifest.get('coverage_contract') != contract:
        raise ValueError('coverage compile scope/flags mismatch')


def run_compile(command, source, env, timeout=1800):
    """Bound only this experiment's compiler; never signal other workloads."""
    def check_space():
        for path in (source, Path(env['RUNNER_TEMP'])):
            if shutil.disk_usage(path).free < 20 * GIB:
                raise ValueError('compile disk headroom below 20 GiB')
    check_space()
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, cwd=source, env=env, start_new_session=True)
    try:
        while True:
            try:
                code = process.wait(timeout=10)
                if code:
                    raise subprocess.CalledProcessError(code, command)
                check_space()
                return
            except subprocess.TimeoutExpired:
                check_space()
                if time.monotonic() >= deadline:
                    raise TimeoutError('compile-only deadline exceeded')
    finally:
        # The pgid is the child created above, not a runner/global selector.
        # A leader can exit before a compiler descendant, so clean the group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=5)


def cache_environment():
    value = os.environ.get('CS_CACHE_ENV', '')
    if value not in ('mty', 'jed'):
        raise ValueError('explicit authorized cache environment required')
    return value


def fingerprint(source):
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip() != SOURCE:
        raise ValueError('unexpected source')
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source):
        raise ValueError('modified tracked source')
    tools = {}
    for name, command in {'go': ['go', 'version'], 'cc': ['cc', '--version'],
                          'cxx': ['c++', '--version'], 'cmake': ['cmake', '--version']}.items():
        tools[name] = subprocess.check_output(command, text=True).splitlines()[0]
    cpu = {}
    for line in Path('/proc/cpuinfo').read_text().split('\n\n', 1)[0].splitlines():
        key, _, value = line.partition(':')
        if key.strip() in ('vendor_id', 'cpu family', 'model', 'model name', 'stepping', 'flags'):
            cpu[key.strip()] = value.strip()
    return {'schema': 1, 'source': SOURCE, 'cs_environment': cache_environment(), 'tools': tools, 'cpu': cpu,
            'os': sha256(Path('/etc/os-release')),
            'platform': subprocess.check_output(['uname', '-sm'], text=True).strip(),
            'go_env': json.loads(subprocess.check_output(
                ['go', 'env', '-json', 'GOOS', 'GOARCH', 'GOAMD64', 'CGO_ENABLED', 'GOEXPERIMENT'], text=True)),
            'workspace': str(source), 'gomodcache': os.environ['GOMODCACHE'],
            'gocache': os.environ['GOCACHE'], 'group': os.environ['COVERAGE_GROUP'],
            'run': os.environ['GITHUB_RUN_ID'], 'attempt': os.environ['GITHUB_RUN_ATTEMPT'],
            'policy': sha256(Path(__file__))}


def check_manifest(manifest, expected):
    if manifest['fingerprint'] != expected:
        raise ValueError('build cache compatibility mismatch')
    size = manifest['unpacked_bytes']
    if not isinstance(size, int) or not 0 < size <= 8 * GIB:
        raise ValueError('cache size outside safety limit')


def validate_entries(entries, expected_size):
    members_safe(entries)
    if len(entries) > 100000 or sum(m.size for m in entries) != expected_size:
        raise ValueError('archive size/member budget mismatch')


def native_inputs(source):
    """The tracked input scope used by MO's native provenance protocol."""
    names = subprocess.check_output(
        ['git', 'ls-files', '-z', '--', 'Makefile', 'cgo', 'thirdparties'], cwd=source)
    return [name.decode() for name in names.split(b'\0') if name]


def snapshot_native_inputs(source):
    result = {}
    for name in native_inputs(source):
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('unsupported native input')
        result[name] = {'sha256': sha256(path), 'mtime_ns': path.stat().st_mtime_ns}
    if not result:
        raise ValueError('empty native input scope')
    return result


def restore_native_input_times(source, recorded):
    """Restore build ordering, never content; validate the entire set first.

    Fresh checkouts assign new mtimes to unchanged sources. Keeping archived
    object mtimes but not source mtimes makes make rebuild a proven generation.
    Do not touch outputs or the provenance stamp to hide an actual rebuild.
    """
    if set(recorded) != set(native_inputs(source)):
        raise ValueError('native input scope mismatch')
    for name, entry in recorded.items():
        path = source / name
        if (path.is_symlink() or any(p.is_symlink() for p in path.parents)
                or not path.is_file() or sha256(path) != entry['sha256']):
            raise ValueError('native input content mismatch')
        timestamp = entry['mtime_ns']
        if type(timestamp) is not int or not 0 < timestamp <= time.time_ns():
            raise ValueError('invalid native input time')
    for name, entry in recorded.items():
        path = source / name
        os.utime(path, ns=(path.stat().st_atime_ns, entry['mtime_ns']))


def verify_native_hashes(source, hashes, allow_stamp_refresh=False):
    for name, digest in hashes.items():
        if allow_stamp_refresh and name == 'cgo/.mo-native-provenance':
            continue
        if sha256(source / name) != digest:
            raise ValueError('native bytes changed; refusing Go build-cache reuse')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['seed', 'restore', 'verify-native', 'verify-scope'])
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--packages', type=Path)
    parser.add_argument('--coverpkg-file', type=Path)
    parser.add_argument('--full-packages', type=Path)
    args = parser.parse_args()
    source = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    cache = Path(os.environ['GOCACHE'])
    artifact = args.artifact
    expected = fingerprint(source)
    started = time.monotonic()
    manifest_path = artifact / 'manifest.json'
    archive = artifact / 'build-cache.tar.gz'
    contract = None
    if args.mode in ('seed', 'verify-scope'):
        if not all((args.packages, args.coverpkg_file, args.full_packages)):
            parser.error('seed and verify-scope require the three scope files')
        contract = coverage_contract(args.packages, args.coverpkg_file, args.full_packages)
    if args.mode == 'seed':
        if artifact.exists():
            raise ValueError('artifact destination already exists')
        def terminated(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminated)
        # This runs BEFORE any S3/UT execution. Never copy a test-result cache.
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('TEST_', 'CS_S3_', 'MO_CI_S3_', 'AWS_')) or key in ('GH_TOKEN', 'GITHUB_TOKEN'):
                env.pop(key)
        env['CGO_CFLAGS'] = f'-I{source}/cgo -I{source}/thirdparties/install/include'
        env['CGO_LDFLAGS'] = (f'-L{source}/cgo -lmo -Wl,-rpath,{source}/cgo '
                             f'-L{source}/thirdparties/install/lib -lusearch_c '
                             f'-Wl,-rpath,{source}/thirdparties/install/lib -lm')
        binary = Path(os.environ['RUNNER_TEMP']) / 'cache-seed-fileservice.test'
        if binary.exists():
            raise ValueError('seed binary destination exists')
        run_compile(['go', 'test', '-mod=readonly', '-c', '-count=1', '-short', '-tags',
                     'matrixone_test', '-p', '7', '-o', str(binary), './pkg/fileservice'], source, env)
        smoke_compile_seconds = time.monotonic() - started
        # Same group and full -coverpkg list as the real Coverage command. No
        # result cache exists: this snapshot is still before the first test.
        coverage_started = time.monotonic()
        run_compile(coverage_compile_command(contract), source, env)
        coverage_compile_seconds = time.monotonic() - coverage_started
        compile_seconds = time.monotonic() - started
        native = list(regular_files(source / 'thirdparties/install')) + list(regular_files(source / 'lib'))
        native += sorted(p for p in (source / 'cgo').iterdir()
                         if p.is_file() and (p.suffix in ('.o', '.so', '.a') or p.name == '.mo-native-provenance'))
        if any(p.is_symlink() for p in native):
            raise ValueError('native symlink unsupported')
        hashes = {str(p.relative_to(source)): sha256(p) for p in native}
        if 'cgo/libmo.so' not in hashes:
            raise ValueError('native output missing')
        go_files = list(regular_files(cache))
        size = sum(p.stat().st_size for p in native + go_files)
        if size > 8 * GIB or shutil.disk_usage(cache).free < 3 * size + 20 * GIB:
            raise ValueError('insufficient snapshot space')
        artifact.mkdir(mode=0o700)
        with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
            for path in native:
                tar.add(path, arcname='native/' + str(path.relative_to(source)), recursive=False)
            for path in go_files:
                tar.add(path, arcname='gocache/' + str(path.relative_to(cache)), recursive=False)
        manifest = {'fingerprint': expected, 'native_hashes': hashes,
                    'native_inputs': snapshot_native_inputs(source),
                    'unpacked_bytes': size, 'archive_bytes': archive.stat().st_size,
                    'archive_sha256': sha256(archive), 'compile_only': True,
                    'coverage_contract': contract,
                    'smoke_compile_seconds': round(smoke_compile_seconds, 3),
                    'coverage_compile_seconds': round(coverage_compile_seconds, 3),
                    'seed_compile_seconds': round(compile_seconds, 3),
                    'pack_hash_seconds': round(time.monotonic() - started - compile_seconds, 3)}
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + '\n')
        print(json.dumps({k: manifest[k] for k in ('unpacked_bytes', 'archive_bytes', 'smoke_compile_seconds',
                         'coverage_compile_seconds', 'seed_compile_seconds', 'pack_hash_seconds')}))
        return
    manifest = json.loads(manifest_path.read_text())
    check_manifest(manifest, expected)
    if not manifest.get('compile_only'):
        raise ValueError('untrusted seed scope')
    if args.mode == 'verify-scope':
        verify_coverage_contract(manifest, contract)
        print(json.dumps({'coverage_compile_scope_valid': True, 'packages': len(contract['packages']),
                          'instrumented_packages': len(contract['coverpkg'])}))
        return
    if args.mode == 'restore':
        if shutil.disk_usage(source).free < manifest['unpacked_bytes'] + 20 * GIB:
            raise ValueError('insufficient restore headroom')
        if cache.exists() or any((source / p).exists() for p in ('thirdparties/install', 'lib', 'cgo/libmo.so')):
            raise ValueError('restore destination not fresh')
        if sha256(archive) != manifest['archive_sha256']:
            raise ValueError('cache checksum mismatch')
        with tarfile.open(archive, 'r:gz') as tar:
            entries = tar.getmembers()
            validate_entries(entries, manifest['unpacked_bytes'])
            for member in entries:
                parts = Path(member.name).parts
                target = (source if parts[0] == 'native' else cache).joinpath(*parts[1:])
                # No links allowed either in archive or pre-existing ancestors.
                if any(p.is_symlink() for p in [target, *target.parents]):
                    raise ValueError('symlink restore target')
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(member.mode & 0o777)
                os.utime(target, (member.mtime, member.mtime))
    verify_native_hashes(source, manifest['native_hashes'], args.mode == 'verify-native')
    if args.mode == 'restore':
        # Only after archive, provenance, and every native output hash pass.
        restore_native_input_times(source, manifest['native_inputs'])
    print(json.dumps({'mode': args.mode, 'seconds': round(time.monotonic() - started, 3),
                      'free_bytes': shutil.disk_usage(source).free, 'native_hashes_valid': True}))


if __name__ == '__main__':
    main()
