"""Bounded per-batch transfer into a private unpublished replica.

Each batch derives its whitelist from immutable trusted metadata and publishes
only complete hash-checked files. A failed batch never publishes the cache.
There are no automatic transport retries or compiler/download calls.
"""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import tarfile

import incremental_cache as c
import node_cache_replication as r

BUDGET=256*1024**2


def batches(plan,catalog,source,lock):
    expected=r.expected_files(plan,catalog,source,lock);groups=[];current=[];size=0
    for name,(length,digest) in sorted(expected.items()):
        if current and size+length>BUDGET:groups.append(current);current=[];size=0
        current.append(name);size+=length
    if current:groups.append(current)
    return expected,groups


def export_batch(shared,catalog,source,lock,number,stream):
    plan=r.make_plan(shared,catalog,source,lock);expected,groups=batches(plan,catalog,source,lock)
    c.require(type(number) is int and 0<=number<len(groups),'batch number differs')
    with tarfile.open(fileobj=stream,mode='w|') as archive:
        raw=c.canonical({'plan':plan,'number':number});info=tarfile.TarInfo('batch.json');info.size=len(raw)
        archive.addfile(info,io.BytesIO(raw))
        for name in groups[number]:
            length,digest=expected[name];path=c.safe_path(shared/name)
            c.require(path.stat().st_size==length and c.digest(path)==digest,'batch source checksum differs')
            info=tarfile.TarInfo(name);info.size=length;info.mode=0o444
            with c.safe_open(path) as body:archive.addfile(info,body)


def receive_batch(shared,catalog,source,lock,target,number,stream):
    shared=c.safe_path(shared);binding=r.bound_identity(target)
    c.require(target['environment']==source['environment'] and target['node']!=source['node'],'batch target differs')
    c.require(not (shared/'incremental-v2').exists() and not (shared/'toolchains-v1').exists()
              and not (shared/'replication-receipt.json').exists(),'batch destination already published')
    stage=shared/'.replication-stage';journal=shared/'replication-chunks.json'
    with tarfile.open(fileobj=stream,mode='r|') as archive:
        rows=iter(archive);header=next(rows,None)
        c.require(header is not None and header.name=='batch.json' and header.isfile() and 0<header.size<=r.PLAN_LIMIT,'batch header differs')
        def unique(pairs):
            result={}
            for k,v in pairs:c.require(k not in result,'duplicate batch metadata');result[k]=v
            return result
        value=json.load(archive.extractfile(header),object_pairs_hook=unique);plan=value['plan']
        expected,groups=batches(plan,catalog,source,lock)
        c.require(type(number) is int and type(value['number']) is int and value['number']==number and 0<=number<len(groups),'batch identity differs')
        state={'schema':1,'binding':binding,'plan_sha256':r.sha(c.canonical(plan)),'batches':len(groups)}
        if journal.exists():
            old=c.read_json(journal);c.require(old==state,'batch journal identity differs')
            state=old;c.require(stage.is_dir(),'batch stage missing')
        else:
            c.require(number==0 and not stage.exists(),'new batch destination is not fresh')
            c.require(r.shutil.disk_usage(shared).free>=sum(x[0] for x in expected.values())+20*c.GIB,'batch destination reserve')
            stage.mkdir()
            c.atomic_json(journal,state)
        ack=shared/'replication-chunk-receipts'/(str(number)+'.json')
        c.require(not ack.exists(),'batch already acknowledged; reconcile instead of retry')
        wanted=set(groups[number]);seen=set()
        for member in rows:
            c.require(member.isfile() and member.name in wanted and member.name not in seen,'unexpected batch member')
            length,digest=expected[member.name];c.require(member.size==length,'batch member size differs')
            with archive.extractfile(member) as body:c.atomic_file(stage/member.name,body,length,digest)
            seen.add(member.name)
        c.require(seen==wanted,'incomplete batch')
    result={'success':True,'number':number,'batches':len(groups),'files':len(wanted),
            'bytes':sum(expected[n][0] for n in wanted),'compiler_invocations':0,'external_downloads':0}
    c.atomic_json(ack,result);return result


def finish(shared,catalog,source,lock,target):
    shared=c.safe_path(shared);binding=r.bound_identity(target);stage=c.safe_path(shared/'.replication-stage')
    c.require(not (shared/'incremental-v2').exists() and not (shared/'toolchains-v1').exists()
        and not (shared/'replication-receipt.json').exists(),'chunk publication already exists')
    metadata={}
    for entry in r.entries_for(catalog,source):
        for name in ['incremental-v2/indexes/'+entry['index_sha256']+'.json']+['incremental-v2/native/'+v+'.json' for v in entry.get('native_manifests',{}).values()]:
            path=c.safe_path(stage/name);c.require(path.stat().st_size<=c.MAX_INDEX,'chunk metadata budget');metadata[name]=path.read_text()
    plan={'schema':1,'source':source,'tool_lock':lock,'metadata':metadata};expected,groups=batches(plan,catalog,source,lock)
    journal=c.read_json(shared/'replication-chunks.json')
    c.require(journal['schema']==1 and journal['binding']==binding and journal['plan_sha256']==r.sha(c.canonical(plan))
        and journal['batches']==len(groups),'chunk journal differs')
    receipts=c.safe_path(shared/'replication-chunk-receipts')
    c.require({x.name for x in receipts.iterdir()}=={str(i)+'.json' for i in range(len(groups))},'chunks incomplete')
    for i,group in enumerate(groups):
        ack=c.read_json(c.safe_path(receipts/(str(i)+'.json')))
        c.require(ack=={'success':True,'number':i,'batches':len(groups),'files':len(group),
            'bytes':sum(expected[n][0] for n in group),'compiler_invocations':0,'external_downloads':0},'chunk acknowledgement differs')
    actual=set()
    for path in stage.rglob('*'):
        c.require(not path.is_symlink() and (path.is_file() or path.is_dir()),'chunk stage unsafe file')
        if path.is_file():actual.add(path.relative_to(stage).as_posix())
    c.require(actual==set(expected),'chunk payload inventory differs')
    # Check all payload hashes BEFORE writing publication metadata.
    for name,(length,digest) in expected.items():
        path=c.safe_path(stage/name);c.require(path.stat().st_size==length and c.digest(path)==digest,'chunk final checksum differs')
    c.atomic_json(stage/'incremental-v2/node-cache-identity.json',target)
    c.atomic_json(stage/'toolchains-v1'/lock['archive_sha256']/'receipt.json',{'schema':1,'lock':lock,'binding':binding})
    verified=r.verify(stage,plan,catalog,source,lock,target)
    for name in ('incremental-v2','toolchains-v1'):
        c.require(not (shared/name).exists(),'chunk destination appeared');os.rename(stage/name,shared/name)
    c.atomic_json(shared/'replication-receipt.json',{**verified,'source':source,'complete':True})
    return verified


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('plan-batches','export-batch','receive-batch','finish'))
    for key in ('catalog','source','lock'):parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--target-json',type=json.loads);parser.add_argument('--number',type=int)
    parser.add_argument('--shared',type=Path,default=Path('/ci-cache'));a=parser.parse_args()
    catalog,source,lock=(c.read_json(p.resolve()) for p in (a.catalog,a.source,a.lock))
    if a.mode in ('plan-batches','export-batch'):r.n.readonly_mount(a.shared)
    if a.mode=='plan-batches':
        expected,groups=batches(r.make_plan(a.shared,catalog,source,lock),catalog,source,lock)
        print(json.dumps({'batches':len(groups),'bytes':sum(x[0] for x in expected.values()),'files':len(expected)}))
    elif a.mode=='export-batch':export_batch(a.shared,catalog,source,lock,a.number,sys.stdout.buffer)
    elif a.mode=='receive-batch':print(json.dumps(receive_batch(a.shared,catalog,source,lock,a.target_json,a.number,sys.stdin.buffer)))
    else:print(json.dumps(finish(a.shared,catalog,source,lock,a.target_json)))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}),file=sys.stderr)
        raise SystemExit(1)
