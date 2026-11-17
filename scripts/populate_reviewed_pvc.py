#!/usr/bin/env python3
"""Populate a new directory on an owned experimental PVC, never an existing cache.

Run only in the independently verified fill Pod, after successful producer/API
provenance review. The signed download URL arrives on stdin and is never logged.
This module does not create Kubernetes resources or declare a producer trusted.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import zipfile

GIB = 1024 ** 3


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 ** 2), b''):
            h.update(block)
    return h.hexdigest()


def populate(config, root):
    started = time.monotonic()
    entry = config['entry']
    env, group = os.environ['CS_CACHE_ENV'], os.environ['COVERAGE_GROUP']
    require(env in ('mty', 'jed', 'ruh') and group in ('0', '1', '2'), 'invalid fill identity')
    require(entry['environment'] == env and entry['group'] == group, 'entry environment/group mismatch')
    require(entry['fingerprint']['cs_environment'] == env and entry['fingerprint']['group'] == group,
            'fingerprint environment/group mismatch')
    require(entry['source_repository'] == 'matrixorigin/matrixone'
            and entry['source'] == entry['fingerprint']['source'], 'source identity mismatch')
    artifact = entry['artifact']
    require(type(artifact['id']) is int and artifact['id'] > 0, 'invalid artifact identity')
    require(type(artifact['size_in_bytes']) is int and 0 < artifact['size_in_bytes'] <= 8 * GIB,
            'transport size outside budget')
    require(config['url'].startswith('https://'), 'HTTPS required')
    require(root.is_dir() and not root.is_symlink(), 'invalid PVC root')
    dest = root / ('reviewed-' + str(artifact['id']))
    require(not dest.exists() and not dest.is_symlink(), 'destination exists; no overwrite or automatic retry')

    def space():
        require(shutil.disk_usage(root).free >= 20 * GIB, 'PVC free-space guard')

    def copy_stream(src, target, maximum):
        count = 0
        with target.open('xb') as out:
            for block in iter(lambda: src.read(1024 ** 2), b''):
                count += len(block)
                require(count <= maximum, 'stream exceeded reviewed size')
                space()
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        return count

    space()
    dest.mkdir(mode=0o700)
    archive = dest / 'transport.zip'
    with urllib.request.urlopen(config['url'], timeout=60) as response:
        size = copy_stream(response, archive, artifact['size_in_bytes'])
    require(size == artifact['size_in_bytes'] and 'sha256:' + digest(archive) == artifact['digest'],
            'transport checksum or size mismatch')
    download = time.monotonic() - started
    with zipfile.ZipFile(archive) as z:
        require(sorted(z.namelist()) == ['build-cache.tar.gz', 'manifest.json'], 'unexpected archive files')
        for member in z.infolist():
            budget = 2 * 1024 ** 2 if member.filename == 'manifest.json' else 8 * GIB
            require(0 < member.file_size <= budget and not member.is_dir()
                    and (member.external_attr >> 16) & 0o170000 != 0o120000, 'invalid archive member')
            with z.open(member) as src:
                require(copy_stream(src, dest / member.filename, member.file_size) == member.file_size,
                        'archive member size mismatch')
    require(digest(dest / 'manifest.json') == entry['manifest_sha256'], 'manifest checksum mismatch')
    manifest = json.loads((dest / 'manifest.json').read_text())
    require(manifest['compile_only'] is True and manifest['fingerprint'] == entry['fingerprint'],
            'compile-only or reviewed fingerprint mismatch')
    tar = dest / 'build-cache.tar.gz'
    require(tar.stat().st_size == manifest['archive_bytes'] and digest(tar) == manifest['archive_sha256'],
            'compile archive checksum or size mismatch')
    space()
    result = {'artifact_id': artifact['id'], 'source': entry['source'], 'environment': env, 'group': group,
              'download_verify_zip_seconds': round(download, 3),
              'unpack_hash_seconds': round(time.monotonic() - started - download, 3),
              'total_seconds': round(time.monotonic() - started, 3), 'bytes': size, 'verified': True}
    with (dest / 'population.json').open('x') as f:
        json.dump(result, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for path in dest.iterdir():
        path.chmod(0o444)
    dest.chmod(0o555)
    # No global sync, deletion, rename, publication, or mutation of other entries.
    # The owning job's step duration additionally covers final metadata writes.
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(populate(json.load(sys.stdin), Path('/ci-cache'))), flush=True)
    except Exception as exc:
        print('PVC population failed: ' + type(exc).__name__, flush=True)
        raise SystemExit(1)
