#!/usr/bin/env python3
"""Bounded protected publisher Pod entry, driven by a UID-checking controller.

Approval is an immutable ConfigMap, not supplied by a test job. Credentials are
never sent to this Pod. A short-lived signed URL arrives over stdin only for
private staging. Publication requires a second fresh metadata authorization.
"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

import incremental_cache as c
import node_compile_cache as n
import node_cache_bundle as b
import node_cache_transport as t
import publish_node_cache as p

STAGE=Path('/stage')
SHARED=Path('/ci-cache/incremental-v2')


def authorize(entry, request, now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    stamp=dt.datetime.fromisoformat(request['checked_at'])
    c.require(stamp.tzinfo is not None and 0<=(now-stamp).total_seconds()<=120,
              'controller authorization stale')
    expected={f"repos/{entry['producer']['repository']}/actions/runs/{entry['producer']['id']}":request['run'],
              f"repos/{entry['producer']['repository']}/actions/artifacts/{entry['artifact']['id']}":request['artifact']}
    def api(path):
        c.require(path in expected, 'unapproved metadata request')
        return expected[path]
    c.require(n.reviewed_entries({'entries':[entry]},entry['runtime'],api,now)==[entry],
              'controller metadata differs or expired')
    return api


def binding(request, environment, node):
    value=request['binding']
    c.require(set(value)=={'environment','node','node_uid','pvc_uid','pv_uid'} and
              value['environment']==environment and value['node']==node,
              'publication binding differs')
    import re
    for key in ('node_uid','pvc_uid','pv_uid'):
        c.require(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value[key]),
                  'invalid binding identity')
    return value


def validate_url(url):
    target=urllib.parse.urlsplit(url)
    c.require(target.scheme=='https' and target.hostname and not target.username and
              not target.password and target.port in (None,443) and
              (target.hostname.endswith('.blob.core.windows.net') or
               target.hostname.endswith('.githubusercontent.com')), 'unapproved download origin')
    return url


def read_approval(root=Path('/probe')):
    approval=(root/'reviewed-entry.json').resolve(strict=True)
    c.require(root in approval.parents,'approval escaped ConfigMap mount')
    return c.read_json(approval)


def execute(phase,entry,request):
    api=authorize(entry,request)
    identity=binding(request,os.environ['CS_CACHE_ENV'],os.environ['ACTUAL_NODE'])
    c.require(entry['runtime']['environment']==identity['environment'], 'environment mismatch')
    if phase=='stage':
        c.require(not SHARED.exists(), 'initial publication tree exists; do not overwrite or retry')
        url=validate_url(request['url'])
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(NoRedirect)
        index=t.receive(entry,STAGE/'received',STAGE/'candidate',api,
                        url_factory=lambda _:url,open_url=opener.open)
        result={'phase':'stage','success':True,'index_sha256':c.digest(index),
                'shared_cache_modified':False,'compiler_invocations':0}
        c.atomic_json(STAGE/'stage-receipt.json',result)
        return result
    c.require(phase in ('publish','verify'), 'unknown publisher phase')
    c.require(c.read_json(STAGE/'stage-receipt.json')['index_sha256']==entry['index_sha256'],
              'private stage receipt differs')
    marker=SHARED/'node-cache-identity.json'
    if phase=='publish':
        c.require(not SHARED.exists(), 'initial publication tree exists; reconcile, never overwrite')
        report=p.publish(STAGE/'candidate',STAGE/'candidate/index.json',SHARED,entry,api)
        # Identity is visible only after all immutable object/index bytes exist.
        c.atomic_json(marker,identity)
        result={**report,'phase':'publish','success':True,'compiler_invocations':0,
                'binding':{**identity,'marker_sha256':c.digest(marker)}}
        c.atomic_json(STAGE/'publication-receipt.json',result)
        return result
    c.require(c.read_json(marker)==identity, 'published marker differs')
    index_path=SHARED/'indexes'/(entry['index_sha256']+'.json')
    c.require(c.digest(index_path)==entry['index_sha256'], 'published index differs')
    index=c.read_json(index_path);objects={}
    c.require(index['runtime']==entry['runtime'], 'published runtime differs')
    for action,record in index['actions'].items():
        c.check_record(action,record);objects[record['output_id']]=record['size']
    for ident,size in objects.items():
        path=SHARED/'objects'/ident[:2]/ident
        c.require(path.stat().st_size==size and c.digest(path)==ident, 'published object differs')
    return {'phase':'verify','success':True,'object_count':len(objects),'object_bytes':sum(objects.values()),
            'index_sha256':entry['index_sha256'],'binding':{**identity,'marker_sha256':c.digest(marker)}}


if __name__=='__main__':
    try:
        request=json.load(sys.stdin)
        # Kubernetes projects an immutable ConfigMap through an atomic ..data
        # symlink. Resolve ONLY this controller-owned approval path; cache data
        # and archives keep their strict no-symlink checks unchanged.
        print(c.canonical(execute(sys.argv[1],read_approval(),request)).decode())
    except Exception as error:
        import traceback
        print(c.canonical({'success':False,'error_type':type(error).__name__,
              'frames':[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)
                        if Path(f.filename).name.startswith(('node_cache_','publish_node_','incremental_'))],
              'automatic_retry':False}).decode())
        raise SystemExit(1)
