"""Protected native-only append using independently granted PVC parent trust.

Only existing parents use persistent authority. The NEW candidate still needs
fresh successful primary producer, live unexpired transport and every byte hash.
No controller credentials or network access are made available to the writer.
"""
import os
import incremental_cache as c
import node_compile_cache as cache
import node_cache_pvc_authority as pvc
import node_cache_profile_append as old
import node_cache_publish_job as approval
import publish_node_cache as publisher


def authorized(catalog,base,entry,request):
    old.check_approval(base,entry)
    candidate_api=approval.authorize(entry,request)
    identity=approval.binding(request,os.environ['CS_CACHE_ENV'],os.environ['ACTUAL_NODE'])
    c.require(identity==base['binding'],'persistent native writer volume changed')
    bound=next(b for b in catalog['nodes'] if {k:b[k] for k in identity}==identity)
    c.require(bound['marker_sha256']==base['marker_sha256'],'persistent native marker changed')
    local=cache.catalog_for_node(catalog,bound)
    c.require(local['entries']==base['existing_entries'],'persistent native parent set omitted a generation')
    metadata=request['persistent_primary_metadata']
    c.require(type(metadata) is dict and len(metadata)<=32,'persistent primary metadata budget')
    def api(path):
        c.require(path in metadata,'missing independent primary persistent metadata')
        return metadata[path]
    authority=pvc.Authority(local,api,lambda:request['live_revocations_git_file'])
    c.require(all(authority.covers(parent) and authority.permits(parent,parent['runtime']) is True
        for parent in base['existing_entries']),'persistent native base not authorized or revoked')
    return candidate_api


def append(candidate,shared,catalog,entry,base,request):
    api=authorized(catalog,base,entry,request);candidate=c.safe_path(candidate);shared=c.safe_path(shared)
    receipt=c.read_json(candidate/'validated-bundle.json')
    c.require(receipt['index_sha256']==entry['index_sha256'] and receipt['manifest_sha256']==entry['manifest_sha256']
        and receipt['producer_id']==entry['producer']['id'] and receipt['artifact_id']==entry['artifact']['id'],
        'native private validation receipt differs')
    path=candidate/'index.json';index=c.read_json(path)
    c.require(c.digest(path)==entry['index_sha256'] and index['schema']==2 and index['actions']=={}
        and index['runtime']==entry['runtime'] and index['source']==entry['source']
        and index['native_generation']==next(iter(entry['native_manifests'])),'native-only index/source differs')
    for mid in entry['native_manifests'].values():
        manifest=c.read_json(candidate/'native'/(mid+'.json'))
        c.require({'cgo/libmo.so','cgo/.mo-native-provenance'}<=set(manifest['outputs']),'native provenance incomplete')
    def check():
        old.verify_base(shared,base,entry)
        authorized(catalog,base,entry,request)
    check()
    result=publisher.publish(candidate,path,shared,entry,api,precondition=check)
    old.verify_base(shared,base,entry,include_candidate=True)
    return {**result,'binding':base['binding'],'marker_sha256':base['marker_sha256'],'compiler_invocations':0,
        'old_metadata_preserved':True,'independently_verified':True,'compatibility_aliases':False,
        'cpu_profile_sha256':old.cpus.checked_cpu(entry['runtime']['cpu'])}
