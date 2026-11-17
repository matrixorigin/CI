#!/usr/bin/env python3
"""Read-only, reviewed, exact-version Coverage cache consumer.

The catalog belongs to the trusted CI workflow revision, never the tested PR.
There is deliberately no cache publication, latest-run search or prefix fallback.
Unavailable/expired/incompatible entries miss before restoration. Integrity errors
fail closed; never continue testing using partially validated build products.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

GIB = 1024 ** 3
FLAGS = ['-mod=readonly', '-count=1', '-json', '-short', '-v', '-tags',
         'matrixone_test', '-timeout=20m', '-p', '7', '-covermode=set']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024 ** 2,
            'invalid cache metadata file')
    return json.loads(path.read_text())


def cache_environment():
    env = os.environ.get('CS_CACHE_ENV', '')
    group = os.environ.get('COVERAGE_GROUP', '')
    require(env == 'mty' and group in ('0', '1'), 'explicit experiment environment/group required')
    suffix = 'rhprep' if env == 'ruh' else 'cpe8k'
    require(os.environ.get('RUNNER_NAME', '').startswith(f'cs-{env}-pvc-g{group}-{suffix}-'), 'runner environment/group mismatch')
    return env


def fingerprint(source):
    """v1 producer-compatible platform identity; no run identity exceptions."""
    def command(args):
        return subprocess.check_output(args, cwd=source, text=True, timeout=30).strip()
    revision = command(['git', 'rev-parse', 'HEAD'])
    require(re.fullmatch('[0-9a-f]{40}', revision), 'invalid checkout revision')
    require(not command(['git', 'status', '--porcelain', '--untracked-files=no']), 'tracked source modified')
    tools = {name: command(args).splitlines()[0] for name, args in {
        'go': ['go', 'version'], 'cc': ['cc', '--version'],
        'cxx': ['c++', '--version'], 'cmake': ['cmake', '--version']}.items()}
    cpu = {}
    for line in Path('/proc/cpuinfo').read_text().split('\n\n', 1)[0].splitlines():
        key, _, value = line.partition(':')
        if key.strip() in ('vendor_id', 'cpu family', 'model', 'model name', 'stepping', 'flags'):
            cpu[key.strip()] = value.strip()
    return {'schema': 1, 'source': revision, 'tools': tools, 'cpu': cpu,
            'os': sha256('/etc/os-release'), 'platform': command(['uname', '-sm']),
            'go_env': json.loads(command(['go', 'env', '-json', 'GOOS', 'GOARCH', 'GOAMD64', 'CGO_ENABLED', 'GOEXPERIMENT'])),
            'workspace': str(source), 'gomodcache': os.environ['GOMODCACHE'],
            'gocache': os.environ['GOCACHE'], 'group': os.environ['COVERAGE_GROUP'],
            'cs_environment': cache_environment(),
            'group_count': 2,
            'partition_policy': sha256(Path(__file__).with_name('coverage_two_groups.py'))}


def compatibility(producer_fingerprint):
    return {k: v for k, v in producer_fingerprint.items() if k not in ('run', 'attempt', 'policy')}


def select_entry(catalog, source_repo, source_sha, environment, group):
    require(catalog.get('schema') == 1 and isinstance(catalog.get('entries'), list), 'unsupported cache catalog')
    candidates = [e for e in catalog['entries'] if (e['source_repository'], e['source'], e['environment'], e['group'])
                  == (source_repo, source_sha, environment, str(group))]
    require(len(candidates) <= 1, 'ambiguous reviewed cache entry')
    return candidates[0] if candidates else None


def validate_provenance(entry, run, artifact):
    p = entry['producer']
    for key in ('id', 'head_sha', 'head_branch', 'event', 'path', 'workflow_id', 'run_attempt'):
        require(run.get(key) == p[key], 'cache producer identity mismatch: ' + key)
    require(run['repository']['full_name'] == p['repository'], 'cache producer repository mismatch')
    require(run['actor']['login'] == p['actor'] and run['triggering_actor']['login'] == p['actor'], 'cache producer actor mismatch')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'cache producer not successful')
    actual_refs = {(r['path'], r['sha']) for r in run.get('referenced_workflows', [])}
    require(all((r['path'], r['sha']) in actual_refs for r in p['referenced_workflows']), 'cache producer workflow revision mismatch')
    for key in ('id', 'name', 'size_in_bytes', 'digest'):
        require(artifact.get(key) == entry['artifact'][key], 'cache artifact identity mismatch: ' + key)
    require(artifact.get('workflow_run', {}).get('id') == p['id'], 'cache artifact run mismatch')
    require(artifact['workflow_run'].get('head_sha') == p['head_sha'], 'cache artifact caller mismatch')


def api(path):
    # Path comes only from the CI-owned catalog; never accept an arbitrary URL.
    require(re.fullmatch(r'repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/(runs|artifacts)/[0-9]+', path), 'invalid GitHub API path')
    token = os.environ.get('CS_CACHE_READ_TOKEN', '')
    if not token:
        raise PermissionError('cache read token unavailable')
    req = urllib.request.Request('https://api.github.com/' + path,
                                 headers={'Authorization': 'Bearer ' + token,
                                          'Accept': 'application/vnd.github+json'})
    # No redirects for authenticated metadata reads.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        data = response.read(2 * 1024 ** 2 + 1)
    require(len(data) <= 2 * 1024 ** 2, 'cache API response too large')
    return json.loads(data)


def safe_relative(name):
    p = PurePosixPath(name)
    require(bool(name) and not p.is_absolute() and '..' not in p.parts and str(p) == name,
            'invalid cache path')
    return p


def native_allowed(name):
    p = safe_relative(name)
    return ((len(p.parts) >= 3 and p.parts[:2] == ('thirdparties', 'install'))
            or (len(p.parts) >= 2 and p.parts[0] == 'lib')
            or (len(p.parts) == 2 and p.parts[0] == 'cgo'
                and (p.suffix in ('.o', '.so', '.a') or p.name == '.mo-native-provenance')))


def native_inputs(source):
    raw = subprocess.check_output(['git', 'ls-files', '-z', '--', 'Makefile', 'cgo', 'thirdparties'], cwd=source)
    return [n.decode() for n in raw.split(b'\0') if n]


def check_native_inputs(source, recorded):
    require(bool(recorded) and set(recorded) == set(native_inputs(source)), 'native input set mismatch')
    for name, meta in recorded.items():
        safe_relative(name)
        path = source / name
        require(path.is_file() and not any(p.is_symlink() for p in [path, *path.parents]), 'native input is not a regular file')
        require(sha256(path) == meta['sha256'], 'native input content mismatch')
        require(type(meta['mtime_ns']) is int and 0 < meta['mtime_ns'] <= time.time_ns(), 'invalid native input timestamp')


def verify_native(source, manifest, stamp_refresh=False):
    require('cgo/libmo.so' in manifest['native_hashes'], 'native library missing from manifest')
    for name, digest in manifest['native_hashes'].items():
        require(native_allowed(name), 'native output not allowlisted')
        if stamp_refresh and name == 'cgo/.mo-native-provenance':
            continue  # make's original prepare/begin/record may update stamp metadata only.
        path = source / name
        require(path.is_file() and not any(p.is_symlink() for p in [path, *path.parents]), 'invalid native output')
        require(sha256(path) == digest, 'native bytes changed; refusing build-cache reuse')


def load_manifest(entry, artifact, current):
    path = artifact / 'manifest.json'
    m = read_json(path)
    require(sha256(path) == entry['manifest_sha256'], 'reviewed manifest checksum mismatch')
    require(m.get('compile_only') is True, 'cache is not compile-only')
    require(m['fingerprint'] == entry['fingerprint'], 'reviewed fingerprint mismatch')
    require(compatibility(m['fingerprint']) == current, 'cache runtime fingerprint mismatch')
    require(type(m['unpacked_bytes']) is int and 0 < m['unpacked_bytes'] <= 8 * GIB, 'cache size outside limit')
    require(0 < m['archive_bytes'] <= 8 * GIB, 'cache archive outside limit')
    return m


def restore(source, cache, artifact, m):
    archive = artifact / 'build-cache.tar.gz'
    require(not archive.is_symlink() and archive.stat().st_size == m['archive_bytes'], 'archive size mismatch')
    require(sha256(archive) == m['archive_sha256'], 'archive checksum mismatch')
    require(all(shutil.disk_usage(p).free >= m['unpacked_bytes'] + 20 * GIB for p in (source, cache.parent)), 'insufficient restore space')
    require(not cache.exists(), 'Go cache destination is not fresh')
    require(not any((source / p).exists() for p in ('thirdparties/install', 'lib', 'cgo/libmo.so')), 'native destination is not fresh')
    check_native_inputs(source, m['native_inputs'])
    with tarfile.open(archive, 'r:gz') as tar:
        entries, targets, names, total, native_names = [], [], set(), 0, set()
        for member in tar:
            path = safe_relative(member.name)
            require(member.isfile() and len(path.parts) >= 2 and path.parts[0] in ('native', 'gocache'), 'unsupported archive entry')
            require(member.name not in names and len(entries) < 100000 and member.size >= 0, 'archive member budget or duplicate')
            names.add(member.name)
            relative = str(PurePosixPath(*path.parts[1:]))
            if path.parts[0] == 'native':
                require(native_allowed(relative), 'archive native path not allowlisted')
                native_names.add(relative)
            target = (source if path.parts[0] == 'native' else cache) / relative
            require(not target.exists() and not any(p.is_symlink() for p in [target, *target.parents]), 'restore would overwrite existing path')
            total += member.size
            require(total <= m['unpacked_bytes'], 'archive expanded size exceeds manifest')
            entries.append(member)
            targets.append(target)
        require(total == m['unpacked_bytes'] and native_names == set(m['native_hashes']), 'archive size or native set mismatch')
        # Validate the complete archive before creating any native/cache file.
        for member, target in zip(entries, targets):
            require(not any(p.is_symlink() for p in [target, *target.parents]), 'restore ancestor changed')
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(member.mode & 0o777)
            os.utime(target, (member.mtime, member.mtime))
    verify_native(source, m)
    # Preserve known source/output ordering only after every content check.
    for name, meta in m['native_inputs'].items():
        p = source / name
        os.utime(p, ns=(p.stat().st_atime_ns, meta['mtime_ns']))


def verify_scope(m, selected, full, cover):
    contract = {'packages': selected.read_text().splitlines(), 'full_packages': full.read_text().splitlines(),
                'coverpkg': cover.read_text().strip().split(','), 'flags': FLAGS}
    require(contract == m['coverage_contract'], 'cache coverage scope or flags mismatch')


def output(name, value):
    require(re.fullmatch('[a-z_]+', name) and '\n' not in str(value), 'invalid cache step output')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(f'{name}={value}\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['select', 'restore', 'verify-native', 'verify-scope'])
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--packages', type=Path)
    parser.add_argument('--full-packages', type=Path)
    parser.add_argument('--coverpkg-file', type=Path)
    args = parser.parse_args()
    source = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    started = time.monotonic()
    if args.mode == 'select':
        output('candidate', 'false')
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        runner = os.environ['RUNNER_NAME']
        match = re.match(r'^cs-(mty|mnl2|jed|ruh)-', runner)
        entry = select_entry(read_json(args.catalog), os.environ['CS_CACHE_SOURCE_REPOSITORY'], revision,
                             match[1] if match else '', os.environ['COVERAGE_GROUP'])
        if entry is None:
            print('Coverage cache miss: no reviewed exact-source/environment/group entry')
            return
        current = fingerprint(source)
        if compatibility(entry['fingerprint']) != current:
            print('Coverage cache miss: platform/toolchain/path version mismatch')
            return
        if any(shutil.disk_usage(p).free < 60 * GIB for p in (source, Path(os.environ['GOCACHE']).parent)):
            print('Coverage cache miss: less than 60 GiB restore headroom')
            return
        p = entry['producer']
        try:
            run = api(f"repos/{p['repository']}/actions/runs/{p['id']}")
            artifact = api(f"repos/{p['repository']}/actions/artifacts/{entry['artifact']['id']}")
        except (urllib.error.URLError, TimeoutError, PermissionError):
            # No write/restore has happened. Never print URLs/tokens/error bodies.
            print('Coverage cache miss: reviewed artifact metadata unavailable or read permission missing')
            return
        if artifact.get('expired') is True:
            print('Coverage cache miss: reviewed artifact expired')
            return
        require(artifact.get('expired') is False, 'artifact expiry state missing')
        validate_provenance(entry, run, artifact)
        with args.state.open('x') as f:
            json.dump(entry, f, sort_keys=True)
        output('candidate', 'true')
        output('run_id', p['id'])
        output('artifact_id', entry['artifact']['id'])
        output('repository', p['repository'])
        print('Reviewed Coverage cache provenance accepted; run', p['id'], 'artifact', entry['artifact']['id'])
        return
    entry = read_json(args.state)
    m = load_manifest(entry, args.artifact, fingerprint(source))
    if args.mode == 'restore':
        restore(source, Path(os.environ['GOCACHE']), args.artifact, m)
        output('hit', 'true')
    elif args.mode == 'verify-native':
        verify_native(source, m, stamp_refresh=True)
    else:
        require(all((args.packages, args.full_packages, args.coverpkg_file)), 'missing coverage scope files')
        verify_scope(m, args.packages, args.full_packages, args.coverpkg_file)
    print(json.dumps({'mode': args.mode, 'verified': True, 'seconds': round(time.monotonic() - started, 3)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Fail closed without exposing credential-bearing commands/API responses.
        print('::error::Coverage cache validation failed (' + type(exc).__name__ + ')')
        raise SystemExit(1)
