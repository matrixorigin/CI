"""Narrow recovery of an unpublished copy missing ONLY its trusted tool archive.

No compiler or downloader. Every existing file is independently hashed before
the sole missing file is accepted. Already-published caches are never modified.
Protected orchestration must separately approve original receipt/volume UIDs.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

import incremental_cache as c
import node_cache_replication as r

FOOTER=b'CS_CACHE_TOOL_FRAME_END_V1\n'


class LimitedReader:
    def __init__(self,stream,size):self.stream=stream;self.remaining=size
    def read(self,size=-1):
        if self.remaining==0:return b''
        value=self.stream.read(self.remaining if size<0 else min(size,self.remaining))
        self.remaining-=len(value);return value


def tool_name(lock):
    return 'toolchains-v1/'+lock['archive_sha256']+'/toolchain.tar.gz'


def inspect(shared,catalog,source,lock,target):
    shared=c.safe_path(shared);r.bound_identity(target)
    c.require(target['environment']==source['environment'] and target['node']!=source['node'],
              'partial recovery binding differs')
    c.require(not (shared/'incremental-v2').exists() and not (shared/'toolchains-v1').exists()
              and not (shared/'replication-receipt.json').exists(),'published destination cannot be recovered')
    stage=c.safe_path(shared/'.replication-stage');c.require(stage.is_dir(),'missing private stage')
    metadata={}
    for entry in r.entries_for(catalog,source):
        names=['incremental-v2/indexes/'+entry['index_sha256']+'.json']
        names+=['incremental-v2/native/'+mid+'.json' for mid in entry.get('native_manifests',{}).values()]
        for name in names:
            path=c.safe_path(stage/name);c.require(path.stat().st_size<=c.MAX_INDEX,'partial metadata budget')
            metadata[name]=path.read_text()
    plan={'schema':1,'source':source,'tool_lock':lock,'metadata':metadata}
    expected=r.expected_files(plan,catalog,source,lock);missing=tool_name(lock)
    wanted=set(expected)-{missing};actual=set()
    allowed_dirs={parent.as_posix() for name in expected for parent in Path(name).parents if parent.as_posix()!='.'}
    for path in stage.rglob('*'):
        c.require(not path.is_symlink(),'partial stage symlink')
        c.require(path.is_file() or path.is_dir(),'partial stage special file')
        if path.is_file():actual.add(path.relative_to(stage).as_posix())
        else:c.require(path.relative_to(stage).as_posix() in allowed_dirs,'partial stage unexpected directory')
    c.require(actual==wanted,'partial recovery requires exactly the tool archive missing')
    for name in sorted(wanted):
        size,digest=expected[name];path=c.safe_path(stage/name)
        c.require(path.stat().st_size==size and c.digest(path)==digest,'existing partial content differs')
    c.require(shutil.disk_usage(shared).free>=20*c.GIB+lock['archive_size'],'partial recovery disk reserve')
    return plan,expected


def export_tool(shared,catalog,source,lock,stream,framed=False):
    # make_plan verifies source identity and full trusted tool archive checksum.
    r.make_plan(shared,catalog,source,lock)
    with c.safe_open(c.safe_path(shared/tool_name(lock))) as body:
        shutil.copyfileobj(body,stream,1024*1024)
    if framed:stream.write(FOOTER)


def finish_tool(shared,catalog,source,lock,target,stream,framed=False):
    plan,expected=inspect(shared,catalog,source,lock,target)
    stage=c.safe_path(shared/'.replication-stage');name=tool_name(lock)
    size,digest=expected[name]
    c.atomic_file(stage/name,LimitedReader(stream,size) if framed else stream,size,digest)
    if framed:c.require(stream.read(len(FOOTER))==FOOTER,'tool transport footer differs')
    binding=r.bound_identity(target)
    c.atomic_json(stage/'incremental-v2/node-cache-identity.json',target)
    c.atomic_json(stage/'toolchains-v1'/lock['archive_sha256']/'receipt.json',
                  {'schema':1,'lock':lock,'binding':binding})
    verified=r.verify(stage,plan,catalog,source,lock,target)
    for directory in ('incremental-v2','toolchains-v1'):
        c.require(not (shared/directory).exists(),'destination appeared during partial recovery')
        os.rename(stage/directory,shared/directory)
    c.atomic_json(shared/'replication-receipt.json',{**verified,'source':source,'complete':True})
    return verified


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('inspect','export-tool','finish-tool'))
    for key in ('catalog','source','lock'):parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--target-json',type=json.loads);parser.add_argument('--shared',type=Path,default=Path('/ci-cache'))
    parser.add_argument('--await-ack',action='store_true');parser.add_argument('--framed-tool',action='store_true')
    args=parser.parse_args();read=lambda p:c.read_json(p.resolve())
    catalog,source,lock=map(read,(args.catalog,args.source,args.lock))
    if args.mode=='export-tool':
        r.n.readonly_mount(args.shared);export_tool(args.shared,catalog,source,lock,sys.stdout.buffer,args.framed_tool)
        if args.await_ack:
            sys.stdout.buffer.flush();c.require(sys.stdin.buffer.read(1)==b'A','consumer acknowledgement missing')
    elif args.mode=='inspect':
        _,expected=inspect(args.shared,catalog,source,lock,args.target_json)
        print(json.dumps({'success':True,'verified_files':len(expected)-1,'missing':[tool_name(lock)],
                          'compiler_invocations':0,'external_downloads':0}))
    else:print(json.dumps(finish_tool(args.shared,catalog,source,lock,args.target_json,sys.stdin.buffer,args.framed_tool)))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}),file=sys.stderr)
        raise SystemExit(1)
