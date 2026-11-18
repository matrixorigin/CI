"""Immutable node tool archive; publishers write once, consumers only read.

The reviewed lock is checked into a pinned CI revision. It authenticates the
entire archive, not just the Go/CMake entry points. No network fallback exists.
Compilation objects and this independent tool namespace are never rewritten.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import time
import zipfile

import incremental_cache as c
import node_compile_cache as n
import node_cache_toolchain as t


def validate_lock(lock):
    c.require(lock['schema']==1 and lock['versions']==t.VERSIONS, 'tool lock version differs')
    c.require(re.fullmatch('[0-9a-f]{64}',lock['archive_sha256']) is not None,
              'tool archive identity invalid')
    c.require(0<lock['archive_size']<512*1024**2, 'tool archive size invalid')
    p=lock['producer'];a=lock['artifact']
    c.require(p['repository']=='matrixorigin/CI' and p['event']=='push' and p['attempt']==1 and
              p['workflow_path']=='.github/workflows/node-cache-canary.yaml' and
              re.fullmatch('[0-9a-f]{40}',p['sha']) is not None, 'tool producer not approved')
    c.require(re.fullmatch('sha256:[0-9a-f]{64}',a['digest']) is not None and
              0<a['size']<512*1024**2 and a['name']=='reviewed-toolchain-contract-'+str(p['id']),
              'tool artifact identity invalid')


def authorize(lock,run,artifact):
    validate_lock(lock);p=lock['producer'];a=lock['artifact']
    c.require(run['id']==p['id'] and run['head_sha']==p['sha'] and run['head_branch']==p['branch'] and
              run['event']==p['event'] and run['path']==p['workflow_path'] and
              run['workflow_id']==p['workflow_id'] and run['run_attempt']==p['attempt'] and
              run['actor']['login']==p['actor'] and run['status']=='completed' and
              run['conclusion']=='success' and run['repository']['full_name']==p['repository'],
              'tool producer provenance differs')
    c.require(artifact['id']==a['id'] and artifact['name']==a['name'] and
              artifact['size_in_bytes']==a['size'] and artifact['digest']==a['digest'] and
              artifact['expired'] is False and artifact['workflow_run']['id']==p['id'] and
              artifact['workflow_run']['head_sha']==p['sha'], 'tool artifact provenance differs')


def binding(catalog,shared,environment,node,hints,readonly):
    if readonly:n.readonly_mount(shared)
    return n.verify_binding(catalog,shared/'incremental-v2',environment,node,hints)


def path_for(shared,lock):
    validate_lock(lock)
    return c.safe_path(shared/'toolchains-v1'/lock['archive_sha256'])


def verify(shared,lock,bound):
    root=path_for(shared,lock)
    receipt=c.read_json(root/'receipt.json')
    c.require(receipt=={'schema':1,'lock':lock,'binding':bound}, 'tool publication receipt differs')
    archive=c.safe_path(root/'toolchain.tar.gz')
    c.require(archive.is_file() and archive.stat().st_size==lock['archive_size'] and
              c.digest(archive)==lock['archive_sha256'], 'persistent tool archive corrupt')
    return archive


def receive(zip_path,stage,lock,catalog_path):
    """Unpack only the one approved archive after authenticating the ZIP."""
    validate_lock(lock);a=lock['artifact']
    c.require(zip_path.stat().st_size==a['size'] and c.digest(zip_path)==a['digest'].split(':')[1],
              'tool transport digest differs')
    c.require(not stage.exists(), 'tool stage exists; reconcile, never overwrite')
    with zipfile.ZipFile(zip_path) as z:
        members=z.infolist()
        c.require(len(members)==1 and members[0].filename=='toolchain.tar.gz' and
                  members[0].file_size==lock['archive_size'] and not members[0].is_dir() and
                  ((members[0].external_attr>>16)&0o170000) in (0,0o100000), 'tool ZIP scope differs')
        c.require(shutil.disk_usage(stage.parent).free>=20*c.GIB+lock['archive_size']+2*c.GIB,
                  'tool stage space reserve')
        stage.mkdir();archive=stage/'toolchain.tar.gz'
        with z.open(members[0]) as source, archive.open('xb') as out:shutil.copyfileobj(source,out)
    # Validate every tar member and exact reviewed executables before publication.
    t.unpack(archive,lock['archive_sha256'],stage/'verified-tools',catalog_path)
    return archive


def publish(shared,lock,bound,archive):
    target=path_for(shared,lock)
    c.require(not target.exists(), 'tool publication exists; reconcile, never overwrite')
    c.require(archive.stat().st_size==lock['archive_size'] and c.digest(archive)==lock['archive_sha256'],
              'tool staging archive changed')
    c.require(shutil.disk_usage(shared).free>=20*c.GIB+lock['archive_size'], 'tool PVC space reserve')
    target.parent.mkdir(exist_ok=True)
    # Exclusive directory, then immutable files. A partial result has no receipt
    # and is rejected by readers; no failure cleanup deletes retained evidence.
    target.mkdir()
    with archive.open('rb') as source,(target/'toolchain.tar.gz').open('xb') as out:
        shutil.copyfileobj(source,out);out.flush();os.fsync(out.fileno())
    c.require(c.digest(target/'toolchain.tar.gz')==lock['archive_sha256'], 'tool publication copy differs')
    c.atomic_json(target/'receipt.json',{'schema':1,'lock':lock,'binding':bound})
    verify(shared,lock,bound)
    return {'archive_sha256':lock['archive_sha256'],'archive_bytes':lock['archive_size'],
            'compiler_invocations':0,'compilation_cache_modified':False}


def restore(shared,lock,catalog_path,environment,node,hints,destination):
    start=time.monotonic();catalog=c.read_json(catalog_path)
    bound=binding(catalog,shared,environment,node,hints,True)
    archive=verify(shared,lock,bound);verified=time.monotonic()
    t.unpack(archive,lock['archive_sha256'],destination,catalog_path)
    return {'persistent_tools':True,'external_tool_downloads':0,'archive_sha256':lock['archive_sha256'],
            'archive_bytes':lock['archive_size'],'binding':bound,
            'verify_seconds':round(verified-start,3),'restore_seconds':round(time.monotonic()-start,3)}


def main():
    parser=argparse.ArgumentParser()
    for key in ('lock','catalog','destination','report'):parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();node=os.environ['CS_CACHE_NODE']
    c.require(node==os.environ['CS_CACHE_EXPECTED_NODE'], 'tool node hint differs')
    report=restore(Path('/ci-cache'),c.read_json(args.lock),args.catalog,os.environ['CS_CACHE_ENV'],node,
                   {k:os.environ['CS_CACHE_'+k.upper()] for k in ('node_uid','pvc_uid','pv_uid')},args.destination)
    t.install(args.destination)
    c.atomic_json(args.report,report)
    print(json.dumps(report))


if __name__=='__main__':main()
