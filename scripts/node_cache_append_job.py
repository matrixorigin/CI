#!/usr/bin/env python3
"""Protected append Pod phases. Only reviewed config and stdin authorization.

Separate from initial publication so an initial publisher can never implicitly
turn an existing directory into an append destination. No compiler or secrets
are available in this Pod. Shared catalog activation remains a later review.
"""
import json
import os
from pathlib import Path
import sys
import urllib.request

import incremental_cache as c
import node_cache_append as append_core
import node_cache_publish_job as initial
import node_cache_transport as transport

STAGE=Path('/stage')
SHARED=Path('/ci-cache/incremental-v2')


def read_base(root=Path('/probe')):
    path=(root/'append-base.json').resolve(strict=True)
    c.require(root in path.parents,'append approval escaped ConfigMap')
    return c.read_json(path)


def execute(phase,entry,base,request):
    c.require(phase in ('stage','append','verify'),'unknown append phase')
    api=initial.authorize(entry,request)
    identity=initial.binding(request,os.environ['CS_CACHE_ENV'],os.environ['ACTUAL_NODE'])
    c.require(base['binding']==identity and entry['runtime']['environment']==identity['environment'],
              'append approved binding differs')
    if phase=='stage':
        append_core.verify_base(SHARED,base,entry['runtime'])
        c.require(entry['index_sha256'] not in base['indexes'],'append already published')
        url=initial.validate_url(request['url'])
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(NoRedirect)
        index=transport.receive(entry,STAGE/'received',STAGE/'candidate',api,
                                url_factory=lambda _:url,open_url=opener.open)
        result={'phase':'stage','success':True,'index_sha256':c.digest(index),
                'shared_cache_modified':False,'compiler_invocations':0}
        c.atomic_json(STAGE/'stage-receipt.json',result)
        return result
    receipt=c.read_json(STAGE/'stage-receipt.json')
    c.require(receipt['success'] is True and receipt['index_sha256']==entry['index_sha256'],
              'append private stage incomplete')
    if phase=='append':
        result={**append_core.append(STAGE/'candidate',SHARED,entry,base,request),
                'phase':'append','success':True}
        c.atomic_json(STAGE/'append-receipt.json',result)
        return result
    receipt=c.read_json(STAGE/'append-receipt.json')
    c.require(receipt['success'] is True and receipt['binding']==identity and
              receipt['index_sha256']==entry['index_sha256'] and
              receipt['marker_sha256']==base['marker_sha256'],'append receipt differs')
    append_core.verify_base(SHARED,base,entry['runtime'],entry['index_sha256'],
                            entry.get('native_manifests',{}).values())
    return {'phase':'verify','success':True,'binding':identity,'index_sha256':entry['index_sha256'],
            'marker_sha256':base['marker_sha256'],'old_metadata_preserved':True,
            'compiler_invocations':0,'independently_verified':True}


if __name__=='__main__':
    try:
        request=json.load(sys.stdin)
        print(c.canonical(execute(sys.argv[1],initial.read_approval(),read_base(),request)).decode())
    except Exception as error:
        print(c.canonical({'success':False,'diagnostic':c.failure_report(error),
                           'automatic_retry':False}).decode())
        raise SystemExit(1)
