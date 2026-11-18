"""Protected CPU-variant publisher phases; controller integration still required."""
import json
import os
from pathlib import Path
import sys
import urllib.request

import incremental_cache as c
import node_cache_append_job as legacy_job
import node_cache_profile_append as core
import node_cache_publish_job as initial
import node_cache_transport as transport

STAGE=Path('/stage')
SHARED=Path('/ci-cache/incremental-v2')


def execute(phase,entry,base,request):
    c.require(phase in ('stage','append','verify'),'unknown CPU append phase')
    api=core.authorized(base,entry,request)
    identity=initial.binding(request,os.environ['CS_CACHE_ENV'],os.environ['ACTUAL_NODE'])
    if phase=='stage':
        core.verify_base(SHARED,base,entry)
        # No writer is available to this phase; staging cannot replace a shared generation.
        url=initial.validate_url(request['url'])
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(NoRedirect)
        index=transport.receive(entry,STAGE/'received',STAGE/'candidate',api,
            url_factory=lambda _:url,open_url=opener.open)
        result={'phase':phase,'success':True,'index_sha256':c.digest(index),
            'shared_cache_modified':False,'compiler_invocations':0}
        c.atomic_json(STAGE/'stage-receipt.json',result);return result
    staged=c.read_json(STAGE/'stage-receipt.json')
    c.require(staged['success'] and staged['index_sha256']==entry['index_sha256'],'CPU private stage incomplete')
    if phase=='append':
        result={**core.append(STAGE/'candidate',SHARED,entry,base,request),'phase':phase,'success':True}
        c.atomic_json(STAGE/'append-receipt.json',result);return result
    written=c.read_json(STAGE/'append-receipt.json')
    c.require(written['success'] and written['binding']==identity and written['index_sha256']==entry['index_sha256']
        and written['marker_sha256']==base['marker_sha256'] and written['compatibility_aliases'] is False,
        'CPU append receipt differs')
    core.verify_base(SHARED,base,entry,include_candidate=True)
    return {'phase':phase,'success':True,'binding':identity,'index_sha256':entry['index_sha256'],
        'marker_sha256':base['marker_sha256'],'compiler_invocations':0,
        'old_metadata_preserved':True,'independently_verified':True,'compatibility_aliases':False}


if __name__=='__main__':
    try:
        print(c.canonical(execute(sys.argv[1],initial.read_approval(),legacy_job.read_base(),json.load(sys.stdin))).decode())
    except Exception as error:
        print(c.canonical({'success':False,'diagnostic':c.failure_report(error),'automatic_retry':False}).decode())
        raise SystemExit(1)
