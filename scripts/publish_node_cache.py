#!/usr/bin/env python3
"""Trusted, offline incremental publisher. Not callable by an ordinary PR job.

The protected control plane must first verify successful producer / artifact API
metadata and transport digest, then pin the candidate index and native manifest
hashes in a reviewed entry. A candidate's self-asserted success is NOT authority.
Consumers mount this tree read-only. Only this separate publisher gets RW.
"""
import argparse
import fcntl
import io
import os
from pathlib import Path
import re
import time

import incremental_cache as c
import node_compile_cache as n


def export_native(source,local,runtime):
    """Freeze native outputs after Make, without compiling again. Source SHA is
    provenance; only native inputs/toolchain/build options choose this key.
    Caller invokes before UT and verifies Make's provenance protocol itself.
    """
    source=c.safe_path(source);local=c.safe_path(local)
    key,inputs=n.native_key(source,runtime);outputs={};total=0
    paths=[]
    for directory in ('cgo','thirdparties/install','lib'):
        base=source/directory
        if base.exists():paths.extend(p for p in base.rglob('*') if p.is_file())
    for path in sorted(paths):
        name=str(path.relative_to(source))
        if not n.legacy.native_allowed(name):continue
        size=path.stat().st_size;digest=c.digest(path)
        c.require(size<=c.MAX_OBJECT,'native object exceeds budget')
        total+=size;c.require(total<=8*c.GIB,'native generation exceeds 8 GiB')
        outputs[name]={'sha256':digest,'size':size,'mode':0o755 if path.stat().st_mode&0o111 else 0o644,
                       'mtime_ns':path.stat().st_mtime_ns}
    c.require('cgo/libmo.so' in outputs and 'cgo/.mo-native-provenance' in outputs,'native provenance missing')
    import shutil
    c.require(shutil.disk_usage(local).free>=3*total+20*c.GIB,'native export headroom insufficient')
    for name,r in outputs.items():
        with c.safe_open(source/name) as body:
            c.atomic_file(local/'objects'/r['sha256'][:2]/r['sha256'],body,r['size'],r['sha256'])
    manifest={'schema':2,'runtime':runtime,'key':key,'compile_only':True,'outputs':outputs,
              'inputs':{name:{'sha256':sha,'mtime_ns':(source/name).stat().st_mtime_ns} for name,sha in inputs.items()}}
    mid=n.hash_value(manifest)
    c.atomic_json(local/'native'/(mid+'.json'),manifest)
    return key,mid


def export_delta(local,runtime,parents,output):
    """Don't re-export shared action mappings that this job merely read."""
    local=c.safe_path(local)
    c.require(not (local/'validation-failed.json').exists(), 'failed cache cannot publish')
    actions={}
    for path in sorted((local/'actions').glob('*/*.json')):
        action=path.stem;record=c.read_json(path,4096);c.check_record(action,record)
        if action in parents:
            c.require(parents[action]==record, 'action differs from trusted parent')
            continue
        obj=local/'objects'/record['output_id'][:2]/record['output_id']
        c.require(obj.stat().st_size==record['size'] and c.digest(obj)==record['output_id'], 'candidate object differs')
        actions[action]=record
        c.require(len(actions)<=c.MAX_ACTIONS, 'too many new actions')
    c.atomic_json(output,{'schema':2,'runtime':runtime,'actions':actions})
    return c.digest(output)


def import_reviewed_disk_cache(disk,local,runtime,output):
    """One-time format conversion, not recompilation, of an audited compile-only
    Go DiskCache v1 archive. Caller verifies its original producer/archive first.
    Unknown formats fail closed. Never point at a random runner's old cache.
    """
    disk=c.safe_path(disk);local=c.safe_path(local)
    cache=c.Cache({'schema':2,'local':str(local),'shared':None,'indexes':[],
                   'runtime':runtime})
    count=0
    for path in sorted(disk.glob('*/*-a')):
        with c.safe_open(path) as f:raw=f.read(176)
        match=re.fullmatch(rb'v1 ([0-9a-f]{64}) ([0-9a-f]{64}) ([ 0-9]{20}) ([ 0-9]{20})\n',raw)
        c.require(match is not None and len(raw)==175, 'unsupported Go disk-cache format')
        action,output_id,size,stamp=match.groups();action=action.decode();output_id=output_id.decode()
        c.require(path.name==action+'-a' and path.parent.name==action[:2] and int(stamp)>0, 'disk-cache action identity differs')
        obj=disk/output_id[:2]/(output_id+'-d')
        # Go may have trimmed a referenced object; omission is an honest miss.
        if not obj.exists():continue
        with c.safe_open(obj) as body:cache.put(action,output_id,int(size),body)
        count+=1;c.require(count<=c.MAX_ACTIONS, 'import action budget exceeded')
    return export_delta(local,runtime,{},output)


def publish(candidate,index_path,shared,reviewed,api=n.legacy.api):
    """Exact reviewed hashes + live producer validation; copy new objects only.

    No catalogue/head is auto-advanced: a trusted caller reviews the returned
    receipt and updates the immutable CI catalogue separately.
    """
    started=time.monotonic();candidate=c.safe_path(candidate);shared=c.safe_path(shared)
    c.require(candidate!=shared and candidate not in shared.parents and shared not in candidate.parents, 'publisher source overlaps shared destination')
    ident=c.digest(index_path)
    c.require(ident==reviewed['index_sha256'], 'candidate index is not approved')
    index=c.read_json(index_path)
    c.require(index['schema']==2 and index['runtime']==reviewed['runtime'], 'candidate runtime differs')
    c.require(len(index['actions'])<=c.MAX_ACTIONS, 'too many actions')
    c.require(n.reviewed_entries({'entries':[reviewed]},index['runtime'],api)==[reviewed], 'producer is unavailable, expired or unapproved')
    objects={};metadata=[(index_path,shared/'indexes'/(ident+'.json'))]
    for action,r in index['actions'].items():
        c.check_record(action,r)
        c.require(r['output_id'] not in objects or objects[r['output_id']]==r['size'], 'object size alias')
        objects[r['output_id']]=r['size']
    for key,mid in reviewed.get('native_manifests',{}).items():
        c.require(c.HEX.fullmatch(key) and c.HEX.fullmatch(mid), 'invalid native identity')
        path=candidate/'native'/(mid+'.json')
        c.require(c.digest(path)==mid, 'native candidate not approved')
        m=c.read_json(path)
        c.require(m['schema']==2 and m['key']==key and m['runtime']==index['runtime'] and m['compile_only'] is True, 'native candidate mismatch')
        for name,r in m['outputs'].items():
            c.require(n.legacy.native_allowed(name), 'native output not allowlisted')
            c.check_record('0'*64,{'output_id':r['sha256'],'size':r['size']})
            c.require(r['sha256'] not in objects or objects[r['sha256']]==r['size'], 'native object alias')
            objects[r['sha256']]=r['size']
        metadata.append((path,shared/'native'/(mid+'.json')))
    c.require(sum(objects.values())<=8*c.GIB, 'publication exceeds 8 GiB budget')
    import shutil
    destination=shared if shared.exists() else shared.parent
    c.require(shutil.disk_usage(destination).free>=20*c.GIB+sum(objects.values()), 'publication disk reserve insufficient')
    # All candidate bytes verified before any publication. Never execute source
    # or build tools in a privileged publisher.
    for digest,size in objects.items():
        src=candidate/'objects'/digest[:2]/digest
        c.require(src.stat().st_size==size and c.digest(src)==digest, 'candidate bytes corrupted')
    shared.mkdir(parents=True,exist_ok=True)
    lock=shared/'.publish.lock'
    fd=os.open(c.safe_path(lock),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    copied=0;deduplicated=0
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for digest,size in objects.items():
            src=candidate/'objects'/digest[:2]/digest;dst=shared/'objects'/digest[:2]/digest
            exists=dst.exists()
            with c.safe_open(src) as body:c.atomic_file(dst,body,size,digest)
            if exists:deduplicated+=size
            else:copied+=size
        # Publish immutable index LAST, after all objects are present. A
        # crash can leave unused objects, never a visible partial index.
        for src,dst in reversed(metadata):
            with c.safe_open(src) as body:c.atomic_file(dst,body,src.stat().st_size,c.digest(src))
    finally:
        os.close(fd)
    return {'index_sha256':ident,'new_bytes':copied,'existing_bytes':deduplicated,
            'publication_seconds':round(time.monotonic()-started,6)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--index',type=Path,required=True);p.add_argument('--shared',type=Path,required=True)
    p.add_argument('--reviewed-entry',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True)
    a=p.parse_args()
    try:c.atomic_json(a.receipt,publish(a.candidate,a.index,a.shared,c.read_json(a.reviewed_entry)))
    except Exception:
        print('Incremental publication rejected; original cache retained.',file=__import__('sys').stderr)
        raise SystemExit(1)


if __name__=='__main__':main()
