"""Compose reviewed native additions without modifying the original catalog.

Private materialization is OUTSIDE cs-node-cache: the original configurator
alone owns the exclusive first creation of that directory. No API/cluster/
shared-cache writes, compilation, expiry renewal or candidate self-approval.
"""
import copy
import hashlib
import os
from pathlib import Path
import sys

import incremental_cache as c
import node_compile_cache as cache
import node_cache_pr_catalog as original
import node_cache_native_append_control as native

RUNS={'mo-standard-0':44391670110,'mo-standard-1':44393215155}
# Independent raw-transport review fixes the entire approval, including expiry
# and the candidate/base identities (not merely the protected receipt body).
APPROVAL_SHA256={
    'mo-standard-0':'41636ad163715f2a187e16ab3460ed0f2091f6824cd26b9ea84c832b8f830bfb',
    'mo-standard-1':'2ab65d0279d3a80c63cbeee41b3d9f861b07da57364232c556e1b1483f630680'}
REVISION='a07808483626a169589783bd7502b3fe60fb8af2'
ORIGINAL_POOL={'uid':'feef57e4-cf58-44f8-a7e2-3b6fb7629f2a',
    'spec_sha256':'da7a82f1589f3bdea722a9a3d7ed4644d2961a641e7c5002a0fa0b47334c957d'}


def compose(base,copied,parent,parent_proof,request,publications):
    original.validate(base,copied,parent,parent_proof)
    c.require(len(parent['entries'])==13 and len(publications)==2,'native publication set incomplete')
    operation=native.NativeOnlyPolicy(request,base,parent_proof)
    policy={'environment':'mty','node_uids':{b['node']:b['node_uid'] for b in parent['nodes'] if b['environment']=='mty'}}
    accepted={}
    for value in publications:
        approval=value['approval'];report=value['report']
        c.require(set(approval)=={'controller_run','artifact','report_sha256','entry','base'},
            'native publication approval schema differs')
        entry=approval['entry'];append_base=approval['base'];node=append_base['binding']['node']
        c.require(node in RUNS and node not in accepted,'unreviewed/duplicate native node')
        c.require(cache.hash_value(approval)==APPROVAL_SHA256[node],
            'independently reviewed native approval changed')
        run=approval['controller_run'];artifact=approval['artifact']
        c.require(run=={'id':RUNS[node],'head_sha':REVISION,'workflow_id':289741451}
            and artifact['name']==f'node-cache-native-append-mty-node-cache-native-append-n{node[-1]}-{RUNS[node]}'
            and type(artifact['id']) is int and artifact['id']>0
            and 0<artifact['size_in_bytes']<=2*1024**2
            and artifact['digest'].startswith('sha256:') and c.HEX.fullmatch(artifact['digest'][7:]),
            'native protected run/transport approval differs')
        # The protected controller writes this exact canonical JSON encoding.
        # Git may pretty-print the committed representation; reconstruction
        # must still reproduce the authenticated RAW state.json byte digest.
        body=c.canonical(report)
        c.require(len(body)<=2*1024**2 and hashlib.sha256(body).hexdigest()==approval['report_sha256']
            and entry['producer']['id']==44387585602 and report['original_pool']==ORIGINAL_POOL,
            'native raw canonical receipt or original pool changed')
        result=operation.verify_receipt(report,entry,append_base,policy,parent)
        c.require(report['append']['new_bytes']+report['append']['existing_bytes']==
            {'mo-standard-0':90119804,'mo-standard-1':90084076}[node],
            'native audited object byte inventory differs')
        c.require(report['append']['new_bytes']==405687,'native unexpectedly rewrote old object inventory')
        binding=append_base['binding'];key=next(iter(entry['native_manifests']))
        accepted[node]={'entry':result['additional_entry'],'authority':{**binding,'native_key':key,
            'manifest_sha256':entry['native_manifests'][key],'index_sha256':entry['index_sha256']}}
    c.require(set(accepted)==set(RUNS),'native two-node receipt coverage incomplete')
    result=copy.deepcopy(parent)
    for node in sorted(RUNS):
        value=accepted[node]
        c.require(value['entry']['index_sha256'] not in {e['index_sha256'] for e in result['entries']},
            'native generation duplicated an existing index')
        identity=lambda row:tuple(row[k] for k in ('environment','node','node_uid','pvc_uid','pv_uid','native_key'))
        c.require(identity(value['authority']) not in {identity(row) for row in result.get('native_authorities',[])},
            'native authority overwrote an existing compatibility choice')
        result['entries'].append(value['entry'])
        result.setdefault('native_authorities',[]).append(value['authority'])
    c.require(len(result['entries'])==15 and result['entries'][:13]==parent['entries']
        and result['nodes']==parent['nodes'],'native composition changed old source/expiry/bindings')
    return result,{'catalog_entries':15,'native_additions':2,'old_entries_and_markers_preserved':True,
        'protected_publication_runs':[RUNS[n] for n in sorted(RUNS)],
        'catalog_canonical_sha256':cache.hash_value(result),'ordinary_pr_precompile':False,'shared_cache_writes':0}


def load(root):
    root=Path(root)
    values=[c.read_json(root/name) for name in ('node-compile-cache-catalog.json',
        'node-cache-all-node-copy-evidence.json','node-compile-cache-pr-catalog.json',
        'node-cache-pr-profile-proof.json','node-native-refresh-request.json')]
    publications=[]
    for number in (0,1):
        prefix=f'node-native-publication-n{number}'
        publications.append({'approval':c.read_json(root/(prefix+'.json')),
            'report':c.read_json(root/(prefix+'-receipt.json'))})
    return compose(*values,publications)


def materialize(root,temp):
    catalog,proof=load(root);temp=c.safe_path(temp)
    target=temp/'cs-reviewed-native-catalog.json'
    c.require(not target.exists(),'private reviewed catalog must be fresh')
    c.require(not (temp/'cs-node-cache').exists(),'per-job cache must be fresh')
    c.atomic_json(target,catalog)
    return target,proof


if __name__=='__main__':
    c.require(sys.argv[1:]==['materialize'],'unknown reviewed native catalog operation')
    target,proof=materialize(Path(__file__).resolve().parents[1]/'config',Path(os.environ['RUNNER_TEMP']))
    print(c.canonical(proof).decode())
