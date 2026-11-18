#!/usr/bin/env python3
"""Transport the pinned toolchain via a trusted job, not release downloads on CS.

The archive SHA travels through the trusted job output. Consumers verify it
before extraction and require exact reviewed executable digests. No compiler,
module cache, credential directory or test output is included.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile

VERSIONS = {'go': '1.27.1', 'cmake': '3.17.2'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def expected(catalog):
    entries = json.loads(catalog.read_text())['entries']
    result = {}
    for tool in VERSIONS:
        hashes = {e['runtime']['tool_binaries'][tool] for e in entries}
        if len(hashes) != 1:
            raise ValueError('reviewed toolchains disagree')
        result[tool] = hashes.pop()
    return result


def check(root, hashes):
    for tool, wanted in hashes.items():
        path = root/tool/'bin'/tool
        if not path.is_file() or path.is_symlink() or digest(path) != wanted:
            raise ValueError('toolchain binary does not match reviewed cache')


def pack(root, output, catalog):
    if output.exists():
        raise ValueError('refusing overwrite')
    check(root, expected(catalog))
    with tarfile.open(output, 'w:gz', compresslevel=1, dereference=True) as tar:
        for tool in VERSIONS:
            tar.add(root/tool, arcname=tool)
    return digest(output)


def unpack(archive, sha, destination, catalog):
    if len(sha) != 64 or digest(archive) != sha:
        raise ValueError('toolchain archive digest mismatch')
    if destination.exists():
        raise ValueError('refusing overwrite')
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts or
                    path.parts[0] not in VERSIONS or member.name in names or
                    not (member.isfile() or member.isdir())):
                raise ValueError('unsafe toolchain member')
            names.add(member.name)
        size = sum(m.size for m in members)
        if size > 2*1024**3 or shutil.disk_usage(destination.parent).free < size+20*1024**3:
            raise ValueError('toolchain space reserve')
        destination.mkdir()
        tar.extractall(destination, filter='data')
    check(destination, expected(catalog))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=('pack', 'restore'))
    p.add_argument('--catalog', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--sha', default='')
    args = p.parse_args()
    if args.mode == 'pack':
        sha = pack(args.root, args.archive, args.catalog)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write('sha256='+sha+'\n')
    else:
        unpack(args.archive, args.sha, args.root, args.catalog)
        # Preserve the original setup-go/setup-cmake locations, not temp paths.
        cache = Path(os.environ['RUNNER_TOOL_CACHE'])
        paths = []
        for tool, version in VERSIONS.items():
            target = cache/tool/version/'x64'
            if target.exists():
                raise ValueError('unexpected existing tool installation')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(args.root/tool), target)
            paths.append(str(target/'bin'))
        with open(os.environ['GITHUB_PATH'], 'a') as f:
            f.write('\n'.join(paths)+'\n')
        with open(os.environ['GITHUB_ENV'], 'a') as f:
            f.write('GOROOT='+str(cache/'go'/VERSIONS['go']/'x64')+'\n')


if __name__ == '__main__':
    main()
