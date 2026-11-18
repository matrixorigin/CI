"""Offline read-only cache/tool proof; writes ONLY its private workspace.

Not a full UT run, compiler-cache hit measurement or concurrency capacity test.
The protected controller validates exact Pod/PVC/PV identities separately.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import re
import subprocess

import incremental_cache as c
import node_cache_replication as replication
import node_compile_cache as cache
import node_cache_toolchain_store as tools


def cpu_profile():
    result={}
    for line in Path('/proc/cpuinfo').read_text().split('\n\n',1)[0].splitlines():
        key,_,value=line.partition(':')
        if key.strip() in ('vendor_id','cpu family','model','model name','stepping','flags'):
            result[key.strip()]=value.strip()
    return result


def check_private(work,tmp,nonce,write=False):
    c.require(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',nonce),'probe identity differs')
    paths=[work/'.private-workspace-proof',tmp/'.private-workspace-proof']
    if write:
        for path in paths:
            with path.open('xb') as file:file.write(nonce.encode())
    c.require(all(path.read_bytes()==nonce.encode() for path in paths),'private workspace contents crossed Pod boundary')
    c.require(os.stat(work).st_dev==os.stat(tmp).st_dev,'temporary writes are not on the workspace filesystem')
    return {'private_marker_verified':True,'tmp_on_work_filesystem':True}


def run(shared,catalog_path,lock_path,environment,node,hints,work,tmp,nonce):
    # Kubernetes projected ConfigMap files are symlinks into an immutable
    # generation. Resolve those instruction paths, never cache data paths.
    catalog_path=catalog_path.resolve();lock_path=lock_path.resolve()
    catalog=c.read_json(catalog_path);lock=c.read_json(lock_path)
    bound=tools.binding(catalog,shared,environment,node,hints,True)
    selected=cache.catalog_for_node(catalog,bound)['entries']
    wanted={cache.hash_value(e['runtime']['cpu']) for e in selected}
    actual=cache.hash_value(cpu_profile())
    c.require(actual in wanted,'actual CPU differs from reviewed compilation profile')
    plan=replication.make_plan(shared,catalog,bound,lock)
    expected=replication.expected_files(plan,catalog,bound,lock)
    for name,(size,digest) in expected.items():
        path=c.safe_path(shared/name)
        c.require(path.stat().st_size==size and c.digest(path)==digest,'shared cache payload changed')
    # The mount and container spec have already been verified read-only before
    # this negative check. No successful cache write is accepted or cleaned up.
    try:
        descriptor=os.open(shared/('.readonly-proof-'+nonce),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except OSError as error:
        c.require(error.errno==errno.EROFS,'cache write rejection was not EROFS')
    else:
        os.close(descriptor);raise ValueError('read-only boundary failed')
    private=check_private(work,tmp,nonce,True)
    restored=tools.restore(shared,lock,catalog_path,environment,node,hints,work/'restored-tools')
    tool_env={**os.environ,'GOTOOLCHAIN':'local','GOPROXY':'off','GOSUMDB':'off','GOROOT':str(work/'restored-tools/go')}
    go=subprocess.check_output([str(work/'restored-tools/go/bin/go'),'version'],env=tool_env,text=True,timeout=30).strip()
    cmake=subprocess.check_output([str(work/'restored-tools/cmake/bin/cmake'),'--version'],env=tool_env,text=True,timeout=30).splitlines()[0]
    c.require(go=='go version go1.27.1 linux/amd64' and cmake=='cmake version 3.17.2','restored tool runtime differs')
    return {'success':True,'auxiliary_probe_only':True,'binding':bound,'cpu_compatible':True,
        'shared_payload_files':len(expected),'shared_payload_bytes':sum(v[0] for v in expected.values()),
        'cache_write_denied_errno':'EROFS','private_work':private,'persistent_tools':restored,
        'go_version':go,'cmake_version':cmake,'compiler_invocations':0,'external_tool_downloads':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('probe','recheck-private'))
    for key in ('catalog','lock'):parser.add_argument('--'+key,type=Path)
    parser.add_argument('--binding-json',type=json.loads);parser.add_argument('--nonce',required=True)
    a=parser.parse_args();work=Path('/home/runner/_work');tmp=Path('/tmp')
    try:
        if a.mode=='recheck-private':result=check_private(work,tmp,a.nonce)
        else:
            identity=a.binding_json
            result=run(Path('/ci-cache'),a.catalog,a.lock,identity['environment'],identity['node'],
                {k:identity[k] for k in ('node_uid','pvc_uid','pv_uid')},work,tmp,a.nonce)
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'success':False,'error_type':type(error).__name__}));raise SystemExit(1)
