"""Offline core for protected append; no cluster access or automatic approval.

The protected controller must supply an immutable reviewed base inventory and
fresh producer authorization. This module is NOT wired to a live workflow yet.
It never changes the node marker or old objects/indexes/native manifests.
"""
import os

import incremental_cache as c
import node_cache_publish_job as job
import publish_node_cache as publisher


def verify_base(shared, approval, runtime, extra_index=None, extra_native=()):
    shared=c.safe_path(shared)
    c.require(shared.is_dir(), 'append requires existing cache')
    c.require(set(approval)=={'binding','marker_sha256','indexes','native_manifests'},
              'append base approval fields differ')
    identity=approval['binding']
    c.require(identity['environment']==runtime['environment'], 'append environment differs')
    marker=shared/'node-cache-identity.json'
    c.require(c.HEX.fullmatch(approval['marker_sha256']) and
              c.digest(marker)==approval['marker_sha256'] and c.read_json(marker)==identity,
              'append node marker differs')
    indexes=approval['indexes'];native=approval['native_manifests']
    for values in (indexes,native):
        c.require(isinstance(values,list) and len(values)<=1024 and
                  len(set(values))==len(values) and all(c.HEX.fullmatch(v) for v in values),
                  'append metadata inventory invalid')
    c.require(bool(indexes), 'append approved base is empty')
    expected_indexes=set(indexes)|({extra_index} if extra_index else set())
    expected_native=set(native)|set(extra_native)
    for directory,expected in (('indexes',expected_indexes),('native',expected_native)):
        actual=list((shared/directory).glob('*'))
        c.require({p.name for p in actual}=={v+'.json' for v in expected},
                  'append metadata inventory changed')
        for path in actual:
            c.require(path.is_file() and not path.is_symlink() and c.digest(path)==path.stem,
                      'append metadata bytes differ')
    actions={};objects={}
    def add(ident,size):
        c.check_record('0'*64,{'output_id':ident,'size':size})
        c.require(ident not in objects or objects[ident]==size, 'append object alias')
        objects[ident]=size
        c.require(len(objects)<=100000, 'append object inventory exceeds budget')
    for ident in sorted(expected_indexes):
        index=c.read_json(shared/'indexes'/(ident+'.json'))
        c.require(index['schema']==2 and index['runtime']==runtime and
                  len(index['actions'])<=c.MAX_ACTIONS, 'append parent runtime differs')
        for action,record in index['actions'].items():
            c.check_record(action,record)
            c.require(action not in actions or actions[action]==record, 'append action conflict')
            actions[action]=record;add(record['output_id'],record['size'])
    for ident in sorted(expected_native):
        manifest=c.read_json(shared/'native'/(ident+'.json'))
        c.require(manifest['schema']==2 and manifest['runtime']==runtime and
                  manifest['compile_only'] is True and c.HEX.fullmatch(manifest['key']),
                  'append native metadata differs')
        for name,record in manifest['outputs'].items():
            c.require(publisher.n.legacy.native_allowed(name), 'append native path differs')
            add(record['sha256'],record['size'])
    for ident,size in objects.items():
        path=shared/'objects'/ident[:2]/ident
        c.require(path.stat().st_size==size and c.digest(path)==ident, 'append base object corrupted')
    return actions


def append(candidate,shared,entry,base,request):
    """Append one fully reviewed, pre-UT delta after full producer success.

    No source SHA/group equality is used as a cache key. Runtime and action
    hashes remain authoritative. Producer metadata is still checked exactly.
    Duplicate candidates require reconciliation, never automatic retry.
    """
    candidate=c.safe_path(candidate);shared=c.safe_path(shared)
    api=job.authorize(entry,request)
    identity=job.binding(request,os.environ['CS_CACHE_ENV'],os.environ['ACTUAL_NODE'])
    c.require(base['binding']==identity, 'append binding differs from approved base')
    validated=c.read_json(candidate/'validated-bundle.json')
    c.require(validated['index_sha256']==entry['index_sha256'] and
              validated['manifest_sha256']==entry['manifest_sha256'] and
              validated['producer_id']==entry['producer']['id'] and
              validated['artifact_id']==entry['artifact']['id'], 'append stage receipt differs')
    index_path=candidate/'index.json'
    c.require(c.digest(index_path)==entry['index_sha256'], 'append candidate index differs')
    index=c.read_json(index_path)
    c.require(entry['index_sha256'] not in base['indexes'], 'append candidate already published')

    def check():
        parents=verify_base(shared,base,entry['runtime'])
        for action,record in index['actions'].items():
            c.check_record(action,record)
            c.require(action not in parents or parents[action]==record, 'append action conflict')
        # Full parent rehash may be slow. Recheck the 120-second controller
        # freshness immediately before publication, including under its lock.
        job.authorize(entry,request)
    check()  # Reject before invoking the writer at all, then repeat under lock.
    report=publisher.publish(candidate,index_path,shared,entry,api,precondition=check)
    verify_base(shared,base,entry['runtime'],entry['index_sha256'],entry.get('native_manifests',{}).values())
    return {**report,'binding':identity,'marker_sha256':base['marker_sha256'],
            'previous_indexes':base['indexes'],'compiler_invocations':0,
            'old_metadata_preserved':True,'independently_verified':True}
