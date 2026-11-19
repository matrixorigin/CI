"""Separately approved writer phases; no compiler, credentials or source execution."""
import json
import sys
from pathlib import Path
import incremental_cache as c
import node_cache_append_job as base_reader
import node_cache_publish_job as approval
import node_cache_profile_append_job as paths
import node_cache_profile_append as verify
import node_cache_persistent_native_append as core
import node_cache_native_selective as selective


def read_catalog(root=Path('/probe')):
    # Only resolve the fixed immutable ConfigMap projection, never cache paths.
    catalog=(root/'persistent-catalog.json').resolve(strict=True)
    c.require(root in catalog.parents,'persistent catalog escaped ConfigMap')
    return c.read_json(catalog)


def execute(phase,catalog,entry,base,request):
    c.require(phase in ('inventory','chunk','stage','append','verify') and entry['native_only'] is True and 'url' not in request,'invalid native-only phase')
    if phase=='chunk':
        # Only immutable private chunk paths named by the authenticated inventory;
        # no cache writes and no credentials or signed URL on this path.
        return selective.put(paths.STAGE,request['object'],request['part'],request['wire'])
    api=core.authorized(catalog,base,entry,request)
    if phase=='inventory':
        verify.verify_base(paths.SHARED,base,entry)
        return selective.begin(paths.SHARED,paths.STAGE,entry,request['index'],request['native'],request['parts'])
    if phase=='stage':
        result=selective.finish(paths.SHARED,paths.STAGE,entry)
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
        print(c.canonical(execute(sys.argv[1],read_catalog(root),approval.read_approval(),
            base_reader.read_base(),json.load(sys.stdin))).decode())
    except Exception as error:
        print(c.canonical({'success':False,'diagnostic':c.failure_report(error),'automatic_retry':False}).decode())
        raise SystemExit(1)
