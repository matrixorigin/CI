"""Build a private cache candidate in a disposable, trusted Linux builder.

The site provides fresh upstream and helper checkouts, the initialized private
cache state and a read-only node cache mount. Never run this inside a PR job or
with publication credentials. No deployment, network dispatch or shared write.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

if __package__:
    from . import cache_refresh_demand as demand
else:
    import cache_refresh_demand as demand


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True, timeout=20).strip()


def checkout(repo, slug, commit):
    demand.require(git(repo, 'remote', 'get-url', 'origin').removesuffix('.git')
                   == 'https://github.com/matrixorigin/' + slug, 'unexpected upstream')
    demand.require(git(repo, 'rev-parse', 'HEAD') == commit, 'unexpected checkout')
    demand.require(not git(repo, 'status', '--porcelain=v1', '--ignored',
                           '--untracked-files=all'), 'builder needs a pristine checkout')


def full_scope(lines):
    packages = [p for p in lines.splitlines()
                if not re.search('driver|engine/aoe|engine/memEngine|pkg/catalog', p)]
    demand.require(packages and len(packages) <= 10000 and len(set(packages)) == len(packages)
                   and all(re.fullmatch(r'github\.com/matrixorigin/matrixone/[A-Za-z0-9_./-]+', p)
                           and '..' not in p.split('/') for p in packages), 'invalid full scope')
    return '\n'.join(packages) + '\n'


def prepare(request, helpers, source, temp, out):
    profile = demand.checked(request['profile'])
    head = request['upstream_commit']
    demand.require(re.fullmatch('[a-f0-9]{40}', head) and request['key'] == demand.digest(profile),
                   'invalid protected request')
    checkout(helpers, 'CI', demand.RECIPE.split(':')[1])
    checkout(source, 'matrixone', head)
    subprocess.run(['git', 'merge-base', '--is-ancestor', head, 'refs/remotes/origin/main'],
                   check=True, cwd=source, timeout=30)
    state = temp/'cs-node-cache/state.json'
    cfg = demand.load(state, 8<<20)
    tools = demand.load(temp/'persistent-toolchain.json', 64<<10)
    binding = tools['binding']
    demand.require(tools['persistent_tools'] is True and binding['environment'] == profile['site']
                   and all(binding[k] == profile[k] for k in ('node','node_uid','pvc_uid','pv_uid'))
                   and Path(cfg['source']).resolve() == source and cfg['config']['shared'] == '/ci-cache/incremental-v2'
                   and demand.digest(cfg['config']['runtime']) == profile['runtime']
                   and cfg['native_key'] == profile['native']
                   and demand.modules(source, head) == profile['modules'], 'builder profile mismatch')
    # The helper checkout is provisioned outside the source tree by the operator.
    demand.require(helpers != source and source not in helpers.parents and helpers not in source.parents,
                   'independent helper checkout required')
    sys.path.insert(0, str(helpers/'scripts'))
    import node_cache_candidate as candidate
    import node_compile_cache as cache
    cache.readonly_mount(Path(cfg['config']['shared']))
    demand.require(Path(os.environ['RUNNER_TEMP']).resolve() == temp and temp not in source.parents
                   and source not in temp.parents and temp != source and out.parent == temp
                   and not out.exists() and not out.is_symlink()
                   and Path(cfg['config']['local']).resolve() == state.parent/'objects-private'
                   and Path(os.environ['GOMODCACHE']).resolve().is_relative_to(temp)
                   and Path(os.environ['GOCACHE']).resolve().is_relative_to(temp)
                   and shlex.split(os.environ['GOCACHEPROG']) ==
                   [sys.executable,str(helpers/'scripts/incremental_cache.py'),'--config',str(state.parent/'config.json')],
                   'private builder paths or cache program differ')
    expected = cache.runtime(source, profile['site'], 'matrixorigin/matrixone')
    demand.require(expected == cfg['config']['runtime'] and
                   cache.native_key(source, expected)[0] == profile['native'], 'live runtime mismatch')
    # Both selected groups use exactly this union and the same instrumentation policy.
    packages = subprocess.check_output(['go','list','-mod=readonly','./...'], cwd=source,
                                       text=True, timeout=600)
    full = temp/'refresh-full.txt'
    cover = temp/'refresh-cover.txt'
    with full.open('x') as f:
        f.write(full_scope(packages))
    with full.open() as inp, cover.open('x') as output:
        subprocess.run(['go','run',str(helpers/'scripts/compact_coverage_profile.go'),'-packages'],
                       stdin=inp, stdout=output, cwd=source, check=True, timeout=600)
    contract = candidate.scope(full, full, cover)
    native = cache.prepare_native(state)
    inc = source/'thirdparties/install/include'
    lib = source/'thirdparties/install/lib'
    os.environ['CGO_CFLAGS'] = f'-I{source}/cgo -I{inc} -DMO_NATIVE_CACHE_{native}=1'
    os.environ['CGO_LDFLAGS'] = f'-L{source}/cgo -lmo -Wl,-rpath,{source}/cgo -L{lib} -lusearch_c -Wl,-rpath,{lib} -lm'
    manifest = candidate.freeze(state, contract, out)
    demand.require(manifest['compile_only'] is True and manifest['source'] == head and
                   manifest['runtime'] == expected, 'private candidate mismatch')
    return dict(operation='prepare',success=True,key=request['key'],upstream_commit=head,
                index_sha256=manifest['index_sha256'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('request','helpers','source','temp','destination'):
        parser.add_argument('--'+name, required=True, type=Path)
    a = parser.parse_args()
    print(json.dumps(prepare(demand.load(a.request, 16384), a.helpers.resolve(), a.source.resolve(),
                             a.temp.resolve(), a.destination.resolve()), sort_keys=True))
