"""Protected multi-CPU append core. NOT wired to a live publisher yet.

Each index/native manifest retains its exact independently approved runtime.
Only CPU may differ across profiles; consumers keep exact-runtime selection.
No compilation, catalog activation, marker update or old-generation rewrite.
"""
import copy
import os

import incremental_cache as c
import node_cache_cpu_inventory as cpus
import node_cache_publish_job as authorization
import node_compile_cache as cache
import publish_node_cache as publisher
from node_cache_workspace_probe_job import cpu_profile


def approval(binding,entries,candidate):
    """Build an immutable review proposal, not self-authorize a producer."""
    identity={k:binding[k] for k in ('environment','node','node_uid','pvc_uid','pv_uid')}
    result={'schema':1,'binding':identity,'marker_sha256':binding['marker_sha256'],
        'existing_entries':entries,'candidate_index_sha256':candidate['index_sha256'],
        'candidate_runtime':candidate['runtime']}
    check_approval(result,candidate)
    return copy.deepcopy(result)


def check_approval(base,entry):
    c.require(set(base)=={'schema','binding','marker_sha256','existing_entries','candidate_index_sha256','candidate_runtime'}
        and base['schema']==1,'CPU append approval schema differs')
    c.require(base['candidate_index_sha256']==entry['index_sha256'] and base['candidate_runtime']==entry['runtime'],
        'CPU candidate differs from independent approval')
    c.require(c.HEX.fullmatch(entry['index_sha256']) and not entry.get('bindings'),
        'CPU candidate index or unpublished status differs')
    c.require(c.HEX.fullmatch(base['marker_sha256']) and base['binding']['environment']==entry['runtime']['environment'],
        'CPU append environment/marker differs')
    cpus.checked_cpu(entry['runtime']['cpu'])
    parents=base['existing_entries'];c.require(0<len(parents)<16,'CPU base entry budget')
    c.require(len({e['index_sha256'] for e in parents})==len(parents),'duplicate CPU base index')
    c.require(entry['index_sha256'] not in {e['index_sha256'] for e in parents},'CPU candidate already approved/published')
    common={k:v for k,v in entry['runtime'].items() if k!='cpu'}
    binding=base['binding']
    for parent in parents:
        c.require(c.HEX.fullmatch(parent['index_sha256']),'CPU parent index identity invalid')
        cpus.checked_cpu(parent['runtime']['cpu'])
        c.require({k:v for k,v in parent['runtime'].items() if k!='cpu'}==common,
            'non-CPU ABI/toolchain/path/build contract changed')
        c.require(cache.catalog_for_node({'entries':[parent]},binding)['entries']==[parent],
            'parent generation was not approved on this volume')


def authorized(base,entry,request):
    check_approval(base,entry)
    candidate_api=authorization.authorize(entry,request)
    identity=authorization.binding(request,os.environ['CS_CACHE_ENV'],os.environ['ACTUAL_NODE'])
    c.require(identity==base['binding'],'CPU publication binding differs')
    parents=request['parent_authorizations'];wanted=base['existing_entries']
    c.require(len(parents)==len(wanted) and len({p['index_sha256'] for p in parents})==len(wanted),
        'parent authorization inventory differs')
    by_id={p['index_sha256']:p for p in parents}
    c.require(set(by_id)=={p['index_sha256'] for p in wanted},'unapproved parent authorization')
    for parent in wanted:
        proof=by_id[parent['index_sha256']]
        c.require(proof['binding']==identity,'parent authorization volume differs')
        authorization.authorize(parent,proof)  # Includes independent fresh API metadata, expiry and provenance.
    return candidate_api


def verify_base(shared,base,entry,*,include_candidate=False):
    check_approval(base,entry);shared=c.safe_path(shared)
    c.require(cpu_profile()==base['candidate_runtime']['cpu'],'actual target CPU differs from approved producer profile')
    marker=shared/'node-cache-identity.json'
    c.require(c.digest(marker)==base['marker_sha256'] and c.read_json(marker)==base['binding'],'CPU append node marker changed')
    entries=base['existing_entries']+([entry] if include_candidate else [])
    indexes={e['index_sha256']:e for e in entries};natives={};objects={};actions={}
    def add(ident,size):
        c.check_record('0'*64,{'output_id':ident,'size':size})
        c.require(ident not in objects or objects[ident]==size,'CPU append object alias')
        objects[ident]=size;c.require(len(objects)<=100000,'CPU append object budget')
    for ident,approved in indexes.items():
        path=shared/'indexes'/(ident+'.json')
        c.require(c.digest(path)==ident,'CPU base index digest differs')
        index=c.read_json(path)
        c.require(index['schema']==2 and index['runtime']==approved['runtime'] and len(index['actions'])<=c.MAX_ACTIONS,
            'CPU base index runtime differs')
        merged=actions.setdefault(cache.hash_value(index['runtime']),{})
        for action,record in index['actions'].items():
            c.check_record(action,record)
            c.require(action not in merged or merged[action]==record,'same-runtime action conflict')
            merged[action]=record;add(record['output_id'],record['size'])
        c.require(len(merged)<=c.MAX_ACTIONS,'CPU merged action budget')
        for key,mid in approved.get('native_manifests',{}).items():
            c.require(c.HEX.fullmatch(key) and c.HEX.fullmatch(mid),'CPU native identity invalid')
            value={'key':key,'runtime':approved['runtime']}
            c.require(mid not in natives or natives[mid]==value,'CPU native metadata alias')
            natives[mid]=value
    for directory,expected in (('indexes',indexes),('native',natives)):
        c.require({p.name for p in (shared/directory).glob('*')}=={ident+'.json' for ident in expected},
            'unreviewed CPU base metadata inventory')
    for ident,approved in natives.items():
        path=shared/'native'/(ident+'.json');c.require(c.digest(path)==ident,'CPU native manifest digest differs')
        m=c.read_json(path)
        c.require(m['schema']==2 and m['runtime']==approved['runtime'] and m['key']==approved['key']
            and m['compile_only'] is True,'CPU native exact runtime differs')
        for name,record in m['outputs'].items():
            c.require(cache.legacy.native_allowed(name),'CPU native output path differs');add(record['sha256'],record['size'])
    for ident,size in objects.items():
        path=shared/'objects'/ident[:2]/ident
        c.require(path.stat().st_size==size and c.digest(path)==ident,'CPU base object corrupt')
    return actions.get(cache.hash_value(entry['runtime']),{})


def append(candidate,shared,entry,base,request):
    api=authorized(base,entry,request);candidate=c.safe_path(candidate);shared=c.safe_path(shared)
    receipt=c.read_json(candidate/'validated-bundle.json')
    c.require(receipt['index_sha256']==entry['index_sha256'] and receipt['manifest_sha256']==entry['manifest_sha256']
        and receipt['producer_id']==entry['producer']['id'] and receipt['artifact_id']==entry['artifact']['id'],
        'CPU private bundle receipt differs')
    index_path=candidate/'index.json';c.require(c.digest(index_path)==entry['index_sha256'],'CPU candidate index differs')
    index=c.read_json(index_path)
    c.require(index['schema']==2 and index['runtime']==base['candidate_runtime'],'CPU candidate exact runtime differs')
    # A genuinely new CPU profile needs its own native output/provenance, not an alias to another CPU's archive.
    if not any(p['runtime']==entry['runtime'] for p in base['existing_entries']):
        c.require(bool(entry.get('native_manifests')),'new CPU profile lacks native generation')
        for mid in entry['native_manifests'].values():
            m=c.read_json(candidate/'native'/(mid+'.json'))
            c.require({'cgo/libmo.so','cgo/.mo-native-provenance'}<=set(m['outputs']),'CPU native provenance incomplete')
    def check():
        parents=verify_base(shared,base,entry)
        for action,record in index['actions'].items():
            c.check_record(action,record)
            c.require(action not in parents or parents[action]==record,'same-runtime candidate action conflict')
        authorized(base,entry,request)  # Recheck 120s metadata freshness AFTER full rehash, also under the writer lock.
    check()
    result=publisher.publish(candidate,index_path,shared,entry,api,precondition=check)
    verify_base(shared,base,entry,include_candidate=True)
    return {**result,'binding':base['binding'],'marker_sha256':base['marker_sha256'],
        'compiler_invocations':0,'old_metadata_preserved':True,'independently_verified':True,
        'cpu_profile_sha256':cpus.checked_cpu(entry['runtime']['cpu']),'compatibility_aliases':False}
