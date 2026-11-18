"""Native-only protected stage; original append/hash verification unchanged."""
import json
import sys
import urllib.request

import incremental_cache as c
import node_cache_append_job as approval
import node_cache_native_transport as transport
import node_cache_profile_append as core
import node_cache_profile_append_job as legacy
import node_cache_publish_job as initial


def execute(phase,entry,base,request):
    c.require(entry.get('native_only') is True,'native-only protected approval missing')
    if phase!='stage':return legacy.execute(phase,entry,base,request)
    api=core.authorized(base,entry,request);core.verify_base(legacy.SHARED,base,entry)
    url=initial.validate_url(request['url'])
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(NoRedirect)
    index=transport.receive(entry,legacy.STAGE/'received',legacy.STAGE/'candidate',api,
        url_factory=lambda _:url,open_url=opener.open)
    result={'phase':'stage','success':True,'index_sha256':c.digest(index),
        'shared_cache_modified':False,'compiler_invocations':0}
    c.atomic_json(legacy.STAGE/'stage-receipt.json',result)
    return result


if __name__=='__main__':
    try:print(c.canonical(execute(sys.argv[1],initial.read_approval(),approval.read_base(),json.load(sys.stdin))).decode())
    except Exception as error:
        print(c.canonical({'success':False,'diagnostic':c.failure_report(error),'automatic_retry':False}).decode())
        raise SystemExit(1)
