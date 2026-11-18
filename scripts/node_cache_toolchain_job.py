"""No compiler, no credentials: receive one reviewed tool archive and publish."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.request

import incremental_cache as c
import node_cache_publish_job as initial
import node_cache_toolchain_store as store

STAGE=Path('/stage')
SHARED=Path('/ci-cache')


def execute(phase,entry,request):
    lock=entry['toolchain'];stamp=dt.datetime.fromisoformat(request['checked_at'])
    c.require(stamp.tzinfo is not None and 0<=(dt.datetime.now(dt.timezone.utc)-stamp).total_seconds()<=120,
              'tool authorization stale')
    store.authorize(lock,request['run'],request['artifact'])
    identity=initial.binding(request,os.environ['CS_CACHE_ENV'],os.environ['ACTUAL_NODE'])
    bound=store.binding(entry['catalog'],SHARED,identity['environment'],identity['node'],identity,False)
    catalog_path=(Path('/probe')/'tool-catalog.json').resolve(strict=True)
    c.require(Path('/probe') in catalog_path.parents,'tool catalog escaped approval')
    if phase=='stage':
        c.require(not store.path_for(SHARED,lock).exists(),'tool target exists; no retry')
        url=initial.validate_url(request['url']);partial=STAGE/'tool-transport.zip.partial'
        size=lock['artifact']['size'];total=0;sha=hashlib.sha256()
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        with urllib.request.build_opener(NoRedirect()).open(url,timeout=60) as response,partial.open('xb') as out:
            c.require(response.status==200 and int(response.headers['Content-Length'])==size,'tool transfer size differs')
            while True:
                block=response.read(1024*1024)
                if not block:break
                total+=len(block)
                c.require(total<=size and shutil.disk_usage(STAGE).free>=20*c.GIB+len(block),'tool transfer reserve')
                out.write(block);sha.update(block)
            out.flush();os.fsync(out.fileno())
        c.require(total==size and 'sha256:'+sha.hexdigest()==lock['artifact']['digest'],'tool ZIP incomplete')
        store.receive(partial,STAGE/'received',lock,catalog_path)
        result={'phase':phase,'success':True,'archive_sha256':lock['archive_sha256'],'compiler_invocations':0}
        c.atomic_json(STAGE/'tool-stage.json',result)
        return result
    c.require(phase in ('append','verify'),'tool phase unknown')
    c.require(c.read_json(STAGE/'tool-stage.json')['archive_sha256']==lock['archive_sha256'],'tool stage receipt differs')
    if phase=='append':
        result=store.publish(SHARED,lock,bound,STAGE/'received/toolchain.tar.gz')
    else:
        store.verify(SHARED,lock,bound)
        result={'archive_sha256':lock['archive_sha256'],'independently_verified':True,
                'compiler_invocations':0,'compilation_cache_modified':False}
    return {**result,'phase':phase,'success':True,'binding':bound}


if __name__=='__main__':
    try:print(c.canonical(execute(sys.argv[1],initial.read_approval(),json.load(sys.stdin))).decode())
    except Exception as error:
        print(c.canonical({'success':False,'diagnostic':c.failure_report(error),'automatic_retry':False}).decode())
        raise SystemExit(1)
