"""DRAFT private composition of fully reviewed native append supplements."""
import copy
from pathlib import Path
import incremental_cache as c
import node_compile_cache as cache
import node_cache_pr_native_catalog as original
import node_cache_pvc_authority as base
import node_cache_native_pvc_authority as native


def compose(source,documents,config):
    c.require(len(source['nodes'])==12 and len(source['entries'])==15,'original reviewed catalog scope differs')
    c.require(set(documents)=={'mty','mnl2','jed','ruh'},'native durable family omitted an environment')
    c.require(not set(source)&{'persistent_pvc_authority','persistent_pvc_authorities'},'family already materialized')
    result=copy.deepcopy(source);values=[]
    for env in ('mty','mnl2','jed','ruh'):
        doc=documents[env]
        c.require(doc['environment']==env,'native durable grant environment differs')
        if env=='mty':base.validate(doc)
        else:
            native.validate(doc,config)
            entry=doc['additional_entry']
            c.require(entry['index_sha256'] not in {e['index_sha256'] for e in result['entries']},'native generation already composed')
            result['entries'].append(copy.deepcopy(entry))
            [(key,manifest)]=entry['native_manifests'].items()
            for binding in doc['base_catalog']['nodes']:
                identity={k:binding[k] for k in ('environment','node','node_uid','pvc_uid','pv_uid')}
                rows=result.setdefault('native_authorities',[])
                c.require(not any(all(row[k]==v for k,v in identity.items()) and row['native_key']==key for row in rows),
                    'refusing to overwrite an existing reviewed native choice')
                rows.append({**identity,'native_key':key,'index_sha256':entry['index_sha256'],'manifest_sha256':manifest})
        c.require(all(b in result['nodes'] for b in doc['base_catalog']['nodes'])
            and all(e in result['entries'] for e in doc['base_catalog']['entries']),
            'durable native document differs from preserved original data')
        values.append({'authorization_id':base.digest(doc),'document':copy.deepcopy(doc)})
    c.require(result['entries'][:15]==source['entries'] and result['nodes']==source['nodes']
        and len(result['entries'])==18,'native supplement changed prior bindings or generations')
    result['persistent_pvc_authorities']=values
    return result


def load(root):
    root=Path(root).resolve();source,proof=original.load(root)
    documents={env:(c.read_json(root/'node-cache-pvc-authorization-mty.json') if env=='mty' else
        native.document(root,env)) for env in ('mty','mnl2','jed','ruh')}
    return compose(source,documents,root),proof


def materialize(root,temp):
    root=Path(root).resolve();temp=c.safe_path(temp)
    oldpath,oldproof=original.materialize(root,temp)
    result,proof=load(root);c.require(proof==oldproof,'native original materialization changed')
    target=temp/'cs-reviewed-persistent-catalog.json'
    c.require(not target.exists() and not (temp/'cs-node-cache').exists(),'native private materialization not fresh')
    c.atomic_json(target,result)
    return {'authorization_ids':{v['document']['environment']:v['authorization_id'] for v in result['persistent_pvc_authorities']},
        'old_catalog_canonical_sha256':base.digest(c.read_json(oldpath)),'catalog_canonical_sha256':base.digest(result),
        'old_entries_and_bindings_unchanged':True,'shared_cache_writes':0,'original_native_composition':proof}


def authority(catalog,api):
    document=catalog['persistent_pvc_authority']['document']
    return native.Authority(catalog,api) if document['schema']==3 else base.Authority(catalog,api)


def preflight(root):
    catalog,proof=load(root);checks=[]
    for binding in catalog['nodes']:
        local=cache.catalog_for_node(catalog,binding);grant=authority(local,cache.legacy.api)
        c.require(all(grant.covers(e) and grant.permits(e,e['runtime']) is True for e in local['entries']),
            'native durable source approval unavailable or revoked')
        checks.extend(grant.publication_metadata_checks)
    return {'metadata_preflight_passed':True,
        'authorization_ids':{v['document']['environment']:v['authorization_id'] for v in catalog['persistent_pvc_authorities']},
        'publication_metadata_checks':checks,'cluster_access':False,'compiler_invocations':0}


if __name__=='__main__':
    import os,sys
    c.require(sys.argv[1:] in (['materialize'],['preflight']),'unknown native durable authority operation')
    root=Path(__file__).resolve().parents[1]/'config'
    print(c.canonical(preflight(root) if sys.argv[1]=='preflight' else materialize(root,Path(os.environ['RUNNER_TEMP']))).decode())
