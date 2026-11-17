#!/usr/bin/env python3
"""Opt-in node-cache consumer; catalog is from the immutable CI helper checkout.

Go owns source/dependency/flag ActionIDs. SHA and partition are provenance, not
whole-cache invalidation keys. Native inputs have an independent exact key.
This process NEVER writes the shared mount and has no publishing credentials.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error

import incremental_cache as c
import cs_coverage_cache as legacy

NATIVE_ENV = ('CC','CXX','AR','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS','OPT_LV',
              'COMMON_CFLAGS','USEARCH_CMAKE_FLAG','SIMSIMD_TARGET','CROARING_CFLAGS',
              'JEMALLOC_CONFIGURE_FLAGS','MUSL','SDKROOT','MACOSX_DEPLOYMENT_TARGET',
              'UNAME_S','UNAME_M','CGO_DEBUG_OPT','MO_CL_CUDA','MO_CL_SIMSIMD',
              'TARGET_SIZE','GO','MAKEFLAGS')


def hash_value(value):
    return hashlib.sha256(c.canonical(value)).hexdigest()


def command(args, source):
    return subprocess.check_output(args,cwd=source,text=True,timeout=30).strip()


def runtime(source, environment, repository):
    c.require(environment in ('mty','mnl2','jed','ruh'), 'unknown cache environment')
    c.require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository), 'invalid source repository')
    c.require(not command(['git','status','--porcelain','--untracked-files=no'],source), 'tracked checkout modified')
    # Reuse the existing strict platform/toolchain/path fingerprint, but not
    # its whole-source SHA / group / writable cache directory identity.
    value=legacy.fingerprint(source)
    for key in ('source','group','gocache'):
        del value[key]
    value.update(schema=2,environment=environment,repository=repository)
    # A package update or compiler replacement at an unchanged path/version
    # string must not silently reuse native output from the previous toolchain.
    value['tool_binaries']={name:c.digest(Path(shutil.which(name)).resolve())
                            for name in ('go','cc','c++','cmake','make','ar','ld')}
    value['system_packages_sha256']=hash_value(command(['dpkg-query','-W','-f=${binary:Package}=${Version}\n'],source))
    return value


def native_key(source, current):
    inputs={name:c.digest(source/name) for name in legacy.native_inputs(source)}
    c.require(inputs and 'Makefile' in inputs, 'native inputs missing')
    c.require(os.environ.get('MO_CL_CUDA','') in ('','0'), 'GPU native profile is not enabled')
    return hash_value({'schema':2,'runtime':current,'inputs':inputs,
                       'build_environment':{k:os.environ.get(k,'') for k in NATIVE_ENV}}),inputs


def readonly_mount(shared):
    """Linux mountinfo is a local mount check, not Kubernetes identity authority."""
    c.require(Path('/proc/self/mountinfo').is_file(), 'node cache requires Linux mount proof')
    matches=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields=line.split()
        mount=fields[4].replace('\\040',' ').replace('\\134','\\')
        if mount == str(shared) or Path(mount) in shared.parents:
            matches.append((len(mount),fields[5].split(',')))
    c.require(matches and 'ro' in max(matches)[1], 'shared cache mount is not read-only')


def verify_binding(catalog, shared, environment, node, hints):
    c.require(catalog.get('schema')==2, 'unsupported node cache catalog')
    nodes=[n for n in catalog['nodes'] if (n['environment'],n['node'])==(environment,node)]
    c.require(len(nodes)==1, 'node has no unique trusted binding')
    binding=nodes[0]
    for key in ('node_uid','pvc_uid','pv_uid'):
        c.require(hints[key]==binding[key], 'runner identity differs from trusted binding')
    marker=shared/'node-cache-identity.json'
    c.require(c.digest(marker)==binding['marker_sha256'], 'node cache identity digest mismatch')
    expected={k:v for k,v in binding.items() if k!='marker_sha256'}
    c.require(c.read_json(marker)==expected, 'node cache identity differs')
    return binding


def reviewed_entries(catalog,current,api=legacy.api,now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    selected=[]
    for entry in catalog['entries']:
        if entry['runtime']!=current:
            continue
        expires=dt.datetime.fromisoformat(entry['expires_at'].replace('Z','+00:00'))
        c.require(expires.tzinfo is not None, 'cache expiry has no timezone')
        if expires<=now:
            continue
        p=entry['producer']
        try:
            run=api(f"repos/{p['repository']}/actions/runs/{p['id']}")
            artifact=api(f"repos/{p['repository']}/actions/artifacts/{entry['artifact']['id']}")
        except (urllib.error.URLError,TimeoutError,PermissionError):
            # No cache was read: unavailable metadata is a normal cold miss.
            continue
        if artifact.get('expired') is True:
            continue
        c.require(artifact.get('expired') is False, 'unknown artifact expiry')
        legacy.validate_provenance(entry,run,artifact)
        c.require(entry.get('compile_only') is True, 'non compile-only cache rejected')
        c.require(p['event'] in ('push','workflow_dispatch') and p['repository']=='matrixorigin/matrixone',
                  'untrusted producer event or repository')
        c.require(c.HEX.fullmatch(entry['index_sha256']), 'invalid reviewed index')
        selected.append(entry)
    c.require(len(selected)<=16, 'too many reviewed cache generations; compact with trusted publisher')
    return selected


def catalog_for_node(catalog, binding):
    """Only generations actually reviewed as published on THIS volume.

    An environment-wide generation is not evidence that every node already has
    its bytes. Unpopulated nodes cold-build; advertised-but-corrupt data fails.
    Marker identity stays immutable when generations are added to the catalog.
    """
    identity = {key: binding[key] for key in ('node_uid', 'pvc_uid', 'pv_uid')}
    entries = []
    for entry in catalog['entries']:
        bindings = entry.get('bindings', [])
        c.require(isinstance(bindings, list) and len(bindings) <= 1000, 'invalid publication bindings')
        for published in bindings:
            c.require(isinstance(published, dict) and set(published) == set(identity),
                      'invalid published volume identity')
        if identity in bindings:
            entries.append(entry)
    return {'entries': entries}


def native_authorities(catalog, binding, selected):
    """Choose a separately reviewed native generation, never silently resolve conflicts.

    Two independent compile-only jobs can produce different timestamp metadata
    for identical inputs. An immutable catalog may explicitly choose ONE of
    those fully reviewed/published manifests; no hashes or provenance are
    discarded. If its producer is expired/unavailable, native compilation is a
    cold miss while other compatible Go objects can still be reused.
    """
    approved=catalog_for_node(catalog,binding)['entries']
    active={e['index_sha256'] for e in selected};result={}
    identity={k:binding[k] for k in ('environment','node','node_uid','pvc_uid','pv_uid')}
    rows=catalog.get('native_authorities',[])
    c.require(isinstance(rows,list) and len(rows)<=1000,'invalid native authority catalog')
    for row in rows:
        c.require(isinstance(row,dict) and set(row)==set(identity)|{'native_key','manifest_sha256','index_sha256'},
                  'invalid native authority identity')
        if any(row[k]!=v for k,v in identity.items()):continue
        key=row['native_key'];mid=row['manifest_sha256'];index=row['index_sha256']
        c.require(all(c.HEX.fullmatch(x) for x in (key,mid,index)) and key not in result,
                  'invalid or duplicate native authority')
        matches=[e for e in approved if e['index_sha256']==index and e.get('native_manifests',{}).get(key)==mid]
        c.require(len(matches)==1,'native authority is not a reviewed published generation')
        result[key]=mid if index in active else None
    return result


def configure(catalog_path,shared,source,local,environment,repository):
    started=time.monotonic()
    shared=c.safe_path(shared);local=c.safe_path(local)
    c.require(not local.exists(), 'per-job cache must be fresh')
    c.require(all(shutil.disk_usage(path).free>=60*c.GIB for path in (source,local.parent)), 'node cache startup headroom below 60 GiB')
    catalog=c.read_json(catalog_path)
    current=runtime(source,environment,repository)
    # A new/uncached node can cold-build. An advertised bound volume must be
    # correct; corruption and identity mismatch are NOT silent misses.
    entries=[];authorities={}
    node=os.environ.get('CS_CACHE_NODE','')
    if shared.exists():
        readonly_mount(shared)
        c.require(node and node==os.environ.get('CS_CACHE_EXPECTED_NODE'), 'node identity missing')
        binding=verify_binding(catalog,shared,environment,node,
            {k:os.environ.get('CS_CACHE_'+k.upper(),'') for k in ('node_uid','pvc_uid','pv_uid')})
        entries=reviewed_entries(catalog_for_node(catalog,binding),current)
        authorities=native_authorities(catalog,binding,entries)
    cfg={'schema':2,'local':str(local/'objects-private'),'shared':str(shared) if shared.exists() else None,
         'indexes':[e['index_sha256'] for e in entries], 'runtime':current}
    cache=c.Cache(cfg)  # Verify every selected index before executing Go.
    key,inputs=native_key(source,current)
    state={'schema':2,'config':cfg,'native_key':key,'native_inputs':inputs,
           'entries':entries,'native_authorities':authorities,'source':str(source),'configured_seconds':round(time.monotonic()-started,6)}
    c.atomic_json(local/'config.json',cfg)
    c.atomic_json(local/'state.json',state)
    helper=Path(__file__).with_name('incremental_cache.py').resolve()
    prog=shlex.join([sys.executable,str(helper),'--config',str(local/'config.json')])
    c.require('\n' not in prog, 'invalid cache command path')
    with open(os.environ['GITHUB_ENV'],'a') as output:
        output.write('GOCACHEPROG='+prog+'\nCS_NODE_CACHE_STATE='+str(local/'state.json')+'\n')
    print('Node compilation cache:', 'reviewed indexes selected' if cfg['indexes'] else 'cold miss; no cache recreation job')
    return state


def native_manifest(state):
    ids={e.get('native_manifests',{}).get(state['native_key']) for e in state['entries']}
    ids.discard(None)
    authorities=state.get('native_authorities',{})
    if state['native_key'] in authorities:
        ident=authorities[state['native_key']]
        if ident is None:return None
        c.require(ident in ids,'selected native authority no longer reviewed')
    else:
        c.require(len(ids)<=1, 'conflicting reviewed native generation')
        if not ids:return None
        ident=ids.pop()
    c.require(c.HEX.fullmatch(ident), 'invalid native manifest ID')
    path=Path(state['config']['shared'])/'native'/(ident+'.json')
    c.require(c.digest(path)==ident, 'native manifest checksum differs')
    m=c.read_json(path)
    c.require(m['schema']==2 and m['key']==state['native_key'] and m['runtime']==state['config']['runtime'], 'native compatibility differs')
    c.require({k:v['sha256'] for k,v in m['inputs'].items()}==state['native_inputs'], 'native input content differs')
    c.require(m.get('compile_only') is True and 'cgo/libmo.so' in m['outputs'], 'incomplete native generation')
    return m


def restore_native(state):
    source=Path(state['source']);m=native_manifest(state)
    if m is None:return False
    key,inputs=native_key(source,state['config']['runtime'])
    c.require(key==state['native_key'] and inputs==state['native_inputs'], 'native inputs changed after selection')
    shared=Path(state['config']['shared'])
    total=0;items=[]
    for meta in m['inputs'].values():
        c.require(type(meta['mtime_ns']) is int and 0<meta['mtime_ns']<=time.time_ns(), 'invalid native input timestamp')
    # Validate the whole output set before touching the private workspace.
    for name,record in m['outputs'].items():
        c.require(legacy.native_allowed(name), 'native output outside allowlist')
        c.check_record('0'*64,{'output_id':record['sha256'],'size':record['size']})
        c.require(record['mode'] in (0o644,0o755) and type(record['mtime_ns']) is int and 0<record['mtime_ns']<=time.time_ns(), 'invalid native metadata')
        target=c.safe_path(source/name);c.require(not target.exists(), 'native destination is not fresh')
        obj=shared/'objects'/record['sha256'][:2]/record['sha256']
        c.require(obj.stat().st_size==record['size'] and c.digest(obj)==record['sha256'], 'native object corrupted')
        total+=record['size'];items.append((obj,target,record))
    import shutil
    c.require(total<=8*c.GIB and shutil.disk_usage(source).free>=20*c.GIB+total, 'native restore space guard')
    for obj,target,r in items:
        with c.safe_open(obj) as f:c.atomic_file(target,f,r['size'],r['sha256'])
        os.chmod(target,r['mode']);os.utime(target,ns=(r['mtime_ns'],r['mtime_ns']))
    for name,meta in m['inputs'].items():
        stamp=meta['mtime_ns']
        c.require(type(stamp) is int and 0<stamp<=time.time_ns(), 'invalid native input timestamp')
        os.utime(source/name,ns=(stamp,stamp))
    return True


def native_digest(source):
    # Go does not track external C library contents. Make the final native
    # bytes part of CGO_CFLAGS, so affected cgo actions/dependents invalidate.
    paths=[]
    for directory in ('cgo','thirdparties/install','lib'):
        path=source/directory
        if path.exists():
            paths.extend(p for p in path.rglob('*') if p.is_file() and p.suffix in ('.a','.so','.dylib','.h','.hpp'))
    c.require((source/'cgo/libmo.so').is_file(), 'native library absent')
    return hash_value({str(p.relative_to(source)):c.digest(p) for p in sorted(paths)})


def prepare_native(state_path):
    state=c.read_json(state_path);source=Path(state['source']);started=time.monotonic()
    restored=restore_native(state)
    if restored:
        subprocess.run(['make','config'],cwd=source,check=True)
        subprocess.run(['make','cgo'],cwd=source,check=True)
        m=native_manifest(state)
        # Keep Make prepare/begin/record and verify produced bytes, not a
        # fabricated provenance stamp. Only its own stamp may be refreshed.
        for name,r in m['outputs'].items():
            if name!='cgo/.mo-native-provenance':
                c.require(c.digest(source/name)==r['sha256'], 'Make changed restored native bytes')
    else:
        subprocess.run([str(Path(__file__).with_name('prepare_coverage_cgo.sh'))],cwd=source,check=True)
    for name in ('xxhash.h','roaring.h','usearch.h'):
        c.require((source/'thirdparties/install/include'/name).is_file(), 'native required header absent')
    value=native_digest(source)
    report={'native_hit':restored,'native_seconds':round(time.monotonic()-started,6),'native_output_sha256':value}
    c.atomic_json(Path(state_path).parent/'native-result.json',report)
    return value


def acceptance(state_path):
    state=c.read_json(state_path);local=Path(state['config']['local'])
    c.require(not (local/'validation-failed.json').exists(), 'cache corruption observed; test success is not accepted')
    stats=[c.read_json(p) for p in (local/'stats').glob('*.json')]
    result={key:sum(s.get(key,0) for s in stats) for key in ('shared_hits','local_hits','misses','puts','copied_bytes','seconds')}
    result['configured_seconds']=state['configured_seconds']
    c.atomic_json(Path(state_path).parent/'acceptance.json',result)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['configure','prepare-native','accept'])
    p.add_argument('--catalog',type=Path);p.add_argument('--shared',type=Path,default=Path('/ci-cache/incremental-v2'))
    p.add_argument('--state',type=Path);a=p.parse_args()
    try:
        if a.mode=='configure':
            configure(a.catalog,a.shared,Path(os.environ['GITHUB_WORKSPACE']).resolve(),
                      Path(os.environ['RUNNER_TEMP'])/'cs-node-cache',os.environ['CS_CACHE_ENV'],
                      os.environ['CS_CACHE_SOURCE_REPOSITORY'])
        elif a.mode=='prepare-native':prepare_native(a.state)
        else:acceptance(a.state)
    except Exception as exc:
        if a.state:
            c.record_failure(a.state.parent, exc)
        print('Node compilation cache validation failed; no fallback after corruption.',file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':main()
