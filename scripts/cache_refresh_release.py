"""Read a protected release projection. Never discover approvals on the cache disk."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
import urllib.error

ROOT = Path('/run/matrixorigin-cache-releases')
SHARED = Path('/ci-cache/incremental-v2')


def select(profile, state, row, revoked, authorities, digest):
    """A release names one verified generation on one exact volume and build profile."""
    if __package__:
        from .cache_refresh_demand import checked, require
    else:
        from cache_refresh_demand import checked, require
    p = checked(profile)
    require(row['schema'] == 1 and row['profile'] == p and row['profile_key'] == digest(p),
            'release belongs to another profile')
    idx, native = row['index_sha256'], row['native_manifests']
    require(re.fullmatch('[0-9a-f]{64}',idx) and re.fullmatch('[0-9a-f]{40}',row['upstream_commit'])
            and re.fullmatch('[0-9a-f]{64}',row['manifest_sha256']) and row['readback_verified'] is True
            and isinstance(native, dict) and set(native) <= {p['native']}
            and all(re.fullmatch('[0-9a-f]{64}',v) for v in native.values()), 'invalid release')
    if (idx in revoked['indexes'] or p['site'] in revoked['environments'] or
        set(revoked['authorization_ids']).intersection([digest(row),*authorities])):
        return None
    require(digest(state['config']['runtime']) == p['runtime'] and state['native_key']==p['native'],
            'runtime changed')
    existing = state.get('native_authorities',{})
    if native and p['native'] in existing and existing[p['native']] != native[p['native']]:
        return None  # never override an existing authority, including explicit denial
    return {'index_sha256':idx,'native_manifests':native}


def install(temp, src, helpers):
    from cache_refresh_demand import modules, checked, digest, require
    sys.path.insert(0,str(helpers/'scripts'))
    import incremental_cache as c
    import node_compile_cache as n
    import node_cache_pvc_authority as pvc
    import node_cache_native_pvc_catalog as catalog
    local = temp/'cs-node-cache'
    state = c.read_json(local/'state.json')
    cfg = state['config']
    shared = SHARED
    require(cfg['shared']==str(shared) and Path(cfg['local'])==local/'objects-private' and
            cfg==c.read_json(local/'config.json') and state['source']==str(src),'wrong private state')
    n.readonly_mount(shared)
    base = c.read_json(temp/'cs-reviewed-persistent-catalog.json')
    b = n.verify_binding(base,shared,os.environ['CS_CACHE_ENV'],os.environ['CS_CACHE_NODE'],
                         {k:os.environ['CS_CACHE_'+k.upper()] for k in ('node_uid','pv_uid','pvc_uid')})
    from cache_refresh_demand import RECIPE
    profile = checked({k:b[k] for k in ('node','node_uid','pv_uid','pvc_uid')} |
                      dict(site=b['environment'],runtime=digest(cfg['runtime']),native=state['native_key'],
                           modules=modules(src,n.command(['git','rev-parse','HEAD'],src)),recipe=RECIPE))
    # Separate Kubernetes ConfigMap mount, never a file selected by PR code or on its cache PVC.
    c.safe_path(ROOT)
    if not ROOT.is_mount():
        return 'no release projection'
    n.readonly_mount(ROOT)
    path = (ROOT/(digest(profile)+'.json')).resolve()
    require(path.is_relative_to(ROOT),'release projection escapes mount')
    if not path.is_file():
        return 'no matching release'
    row = c.read_json(path,16384)
    try:
        authority = catalog.authority(n.catalog_for_node(base,b),n.legacy.api)
    except (urllib.error.URLError,TimeoutError,PermissionError):
        return 'live authority unavailable; no new selection'
    ids = []
    a = authority
    while a is not None:
        ids.append(a.ident)
        a = getattr(a,'parent',None)
    entry = select(profile,state,row,authority.revoked,ids,digest)
    if entry is None:
        return 'release revoked or superseded'
    idx = entry['index_sha256']
    if idx in cfg['indexes']:
        return 'already selected'
    # A privileged release cannot revive a generation rejected by the original selector.
    require(idx not in {e['index_sha256'] for e in n.catalog_for_node(base,b)['entries']},
            'released index conflicts with base catalog')
    original_actions = c.Cache(cfg).index.keys()
    previous_native = n.native_manifest(state) if entry['native_manifests'] else None
    cfg['indexes'] = [*cfg['indexes'],idx]
    state['entries'] = [*state['entries'],entry]
    merged = c.Cache(cfg)  # Rehash the index, check runtime, action conflicts and layer budget.
    if entry['native_manifests']:
        require(n.native_manifest(state) is not None,'released native manifest unavailable')
    # Before the first compilation. Only replace this job's private configuration.
    for name, value in [('config',cfg),('state',state)]:
        fresh = local/(name+'-with-release.json')
        c.atomic_json(fresh,value)
        os.replace(fresh,local/(name+'.json'))
    c.atomic_json(local/'release-selection.json',{'profile_key':digest(profile),'index_sha256':idx,
                  'release_sha256':digest(row),'native_manifests':entry['native_manifests'],
                  'previously_native_readable':previous_native is not None})
    c.atomic_json(local/'release-only-actions.json',sorted(merged.index.keys()-original_actions))
    with open(os.environ['GITHUB_ENV'],'a') as output:
        print('GOCACHEPROG='+shlex.join([sys.executable,'-I',str(Path(__file__).resolve()),
              '--helpers',str(helpers),'--cacheprog']),file=output)
    return 'selected '+idx


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--helpers',type=Path,required=True)
    parser.add_argument('--cacheprog',action='store_true')
    parser.add_argument('--summarize',action='store_true')
    args=parser.parse_args()
    # Invocation uses -I, so only add our pinned sibling modules.
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    helpers=args.helpers.resolve()
    temp=Path(os.environ['RUNNER_TEMP']).resolve()
    local=temp/'cs-node-cache'
    sys.path.insert(0,str(helpers/'scripts'))
    import incremental_cache as c
    if args.cacheprog:
        unique=set(c.read_json(local/'release-only-actions.json'))
        class TracedCache(c.Cache):
            def get(self,action):
                before=self.stats['shared_hits']
                result=super().get(action)
                if action in unique and self.stats['shared_hits']>before:
                    self.stats['released_shared_hits']=self.stats.get('released_shared_hits',0)+1
                return result
        c.Cache=TracedCache
        c.protocol(c.read_json(local/'config.json'),sys.stdin.buffer,sys.stdout.buffer)
    elif args.summarize:
        if (local/'release-selection.json').is_file():
            row=c.read_json(local/'release-selection.json')
            state=c.read_json(local/'state.json')
            nfile=local/'native-result.json'
            native=c.read_json(nfile)['native_hit'] if nfile.is_file() else False
            row['released_native_hit']=bool(native and not row['previously_native_readable']
                                           and state['native_key'] in row['native_manifests'])
            row['released_shared_go_reads']=sum(c.read_json(f).get('released_shared_hits',0)
                      for f in (local/'objects-private/stats').glob('*.json'))
            c.atomic_json(local/'release-consumption.json',row)
    else:
        print(install(temp,Path(os.environ['GITHUB_WORKSPACE']).resolve(),helpers))
