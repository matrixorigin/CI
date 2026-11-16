#!/usr/bin/env python3
"""Run-scoped compile-only seed for the isolated full Coverage comparison."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

from compile_cache_probe import SOURCE, GIB, sha256, members_safe, regular_files


def fingerprint(source):
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip() != SOURCE:
        raise ValueError('unexpected source')
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source):
        raise ValueError('modified tracked source')
    tools = {}
    for name, command in {'go': ['go', 'version'], 'cc': ['cc', '--version'],
                          'cxx': ['c++', '--version'], 'cmake': ['cmake', '--version']}.items():
        tools[name] = subprocess.check_output(command, text=True).splitlines()[0]
    return {'schema': 1, 'source': SOURCE, 'tools': tools,
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['seed', 'restore', 'verify-native'])
    parser.add_argument('--artifact', type=Path, required=True)
    args = parser.parse_args()
    source = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    cache = Path(os.environ['GOCACHE'])
    artifact = args.artifact
    expected = fingerprint(source)
    started = time.monotonic()
    manifest_path = artifact / 'manifest.json'
    archive = artifact / 'build-cache.tar.gz'
    if args.mode == 'seed':
        if artifact.exists():
            raise ValueError('artifact destination already exists')
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
        subprocess.run(['go', 'test', '-mod=readonly', '-c', '-count=1', '-short', '-tags',
                        'matrixone_test', '-p', '7', '-o', str(binary), './pkg/fileservice'],
                       cwd=source, env=env, check=True, timeout=1800)
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
                    'unpacked_bytes': size, 'archive_bytes': archive.stat().st_size,
                    'archive_sha256': sha256(archive), 'compile_only': True,
                    'seed_compile_seconds': round(compile_seconds, 3),
                    'pack_hash_seconds': round(time.monotonic() - started - compile_seconds, 3)}
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + '\n')
        print(json.dumps({k: manifest[k] for k in ('unpacked_bytes', 'archive_bytes', 'seed_compile_seconds', 'pack_hash_seconds')}))
        return
    manifest = json.loads(manifest_path.read_text())
    check_manifest(manifest, expected)
    if not manifest.get('compile_only'):
        raise ValueError('untrusted seed scope')
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
    for name, digest in manifest['native_hashes'].items():
        # make cgo may refresh its provenance stamp, but native content must not change.
        if args.mode == 'verify-native' and name.endswith('.mo-native-provenance'):
            continue
        if sha256(source / name) != digest:
            raise ValueError('native bytes changed; refusing Go build-cache reuse')
    print(json.dumps({'mode': args.mode, 'seconds': round(time.monotonic() - started, 3),
                      'free_bytes': shutil.disk_usage(source).free, 'native_hashes_valid': True}))


if __name__ == '__main__':
    main()
