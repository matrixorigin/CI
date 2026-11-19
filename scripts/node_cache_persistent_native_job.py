"""Separately approved writer phases; no compiler, credentials or source execution."""
import json
import sys
import urllib.request
import incremental_cache as c
import node_cache_append_job as base_reader
import node_cache_publish_job as approval
import node_cache_profile_append_job as paths
import node_cache_profile_append as verify
import node_cache_persistent_native_append as core
import node_cache_native_transport as transport


def execute(phase,catalog,entry,base,request):
    c.require(phase in ('stage','append','verify') and entry['native_only'] is True,'invalid native-only phase')
    api=core.authorized(catalog,base,entry,request)
    if phase=='stage':
        verify.verify_base(paths.SHARED,base,entry)
        url=approval.validate_url(request['url'])
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(NoRedirect)
        index=transport.receive(entry,paths.STAGE/'received',paths.STAGE/'candidate',api,
            url_factory=lambda _:url,open_url=opener.open)
        result={'phase':phase,'success':True,'index_sha256':c.digest(index),'shared_cache_modified':False,'compiler_invocations':0}
        c.atomic_json(paths.STAGE/'stage-receipt.json',result);return result
    staged=c.read_json(paths.STAGE/'stage-receipt.json')
    c.require(staged['success'] is True and staged['index_sha256']==entry['index_sha256'],'native stage receipt differs')
    if phase=='append':
        result={**core.append(paths.STAGE/'candidate',paths.SHARED,catalog,entry,base,request),'phase':phase,'success':True}
        c.atomic_json(paths.STAGE/'append-receipt.json',result);return result
    written=c.read_json(paths.STAGE/'append-receipt.json')
    c.require(written['success'] is True and written['binding']==base['binding'] and written['index_sha256']==entry['index_sha256']
        and written['marker_sha256']==base['marker_sha256'] and written['compatibility_aliases'] is False,'native append receipt differs')
    verify.verify_base(paths.SHARED,base,entry,include_candidate=True)
    return {'phase':phase,'success':True,'index_sha256':entry['index_sha256'],'binding':base['binding'],
        'marker_sha256':base['marker_sha256'],'compiler_invocations':0,'old_metadata_preserved':True,
        'independently_verified':True,'compatibility_aliases':False}


if __name__=='__main__':
    try:
        root=__import__('pathlib').Path('/probe')
        print(c.canonical(execute(sys.argv[1],c.read_json(root/'persistent-catalog.json'),approval.read_approval(),
            base_reader.read_base(),json.load(sys.stdin))).decode())
    except Exception as error:
        print(c.canonical({'success':False,'diagnostic':c.failure_report(error),'automatic_retry':False}).decode())
        raise SystemExit(1)
