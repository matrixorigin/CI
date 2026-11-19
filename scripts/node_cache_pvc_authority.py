"""Repo-reviewed persistent PVC authority, independent of transport retention.

Only independently full-hashed, exact-UID publications in this immutable CI
approval are eligible. Original catalog records never change. A live Git-owned
revocation file is checked separately before every cache configuration.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import urllib.request
import incremental_cache as c
import cs_coverage_cache as legacy

APPROVAL_SHA256='378420acdea75a159fef827b87d460832bf1c4dacb0ac9cfdced6fe2646a03ea'
SCANNER_REVISION='d4009deaad719fbc39a71dbcaf6a4d9f4a6d3ce1'
SCANNER_CONTRACT=44433331964
REVOCATION_PATH='repos/matrixorigin/CI/contents/config/node-cache-pvc-revocations.json?ref=codex/pr-node-cache-two-group-capacity'


def digest(value):return hashlib.sha256(c.canonical(value)).hexdigest()


def revocation_api():
    # One fixed primary GitHub endpoint using the existing read-only metadata
    # credential; avoid anonymous-IP rate limits for a multi-PR runner fleet.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    token=os.environ.get('CS_CACHE_READ_TOKEN','')
    if not token:raise PermissionError('persistent revocation metadata credential unavailable')
    request=urllib.request.Request('https://api.github.com/'+REVOCATION_PATH,
        headers={'Accept':'application/vnd.github+json','Authorization':'Bearer '+token})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
        raw=response.read(65537)
    c.require(len(raw)<=65536,'persistent revocation response too large')
    return json.loads(raw)


def revocations(read=revocation_api):
    value=read()
    c.require(value['type']=='file' and value['path']=='config/node-cache-pvc-revocations.json'
        and value['encoding']=='base64','revocation Git source differs')
    raw=base64.b64decode(value['content'],validate=False)
    c.require(len(raw)<=16384 and hashlib.sha1(('blob '+str(len(raw))+'\0').encode()+raw).hexdigest()==value['sha'],
        'revocation Git blob hash differs')
    result=json.loads(raw)
    c.require(set(result)=={'schema','authorization_ids','indexes','environments'} and result['schema']==1,
        'revocation schema differs')
    for key in ('authorization_ids','indexes'):
        rows=result[key]
        c.require(type(rows) is list and len(rows)<=1000 and len(set(rows))==len(rows)
            and all(type(x) is str and c.HEX.fullmatch(x) for x in rows),'invalid revoked cache identity')
    c.require(type(result['environments']) is list and len(set(result['environments']))==len(result['environments'])
        and set(result['environments'])<=set(('mty','mnl2','jed','ruh')),'invalid revoked environment')
    return result


def validate(document):
    c.require(digest(document)==APPROVAL_SHA256,'persistent approval differs from independent review')
    c.require(document['schema']==1 and document['lifecycle']=='exact-volume-runtime-content-until-revoked',
        'persistent lifetime policy differs')
    report=document['review'];base=document['base_catalog']
    c.require(report['complete'] is True and report['environment']==document['environment']
        and report['revision']==SCANNER_REVISION and report['contract_run']==SCANNER_CONTRACT
        and report['cluster_cache_writes']==report['compiler_invocations']==report['external_downloads']==0
        and report['cache_recreated'] is False and report['automatic_retry'] is False
        and report['helper_pods_absent'] is True and report['catalog_sha256']==document['source_catalog_sha256']
        and not set(report)&{'pending_create','pending_delete','active_phase','error_type','failure_sites'},
        'independent PVC review incomplete/ambiguous')
    pods=[v for v in report['created'] if v['kind']=='Pod']
    c.require(len(pods)==3 and report['deleted']==pods and len(report['created'])==4
        and report['created'][0]['kind']=='ConfigMap','readonly review scope/UID retirement differs')
    targets=report['targets'];c.require(set(targets)=={'mo-standard-0','mo-standard-1','mo-standard-2'},'PVC review omitted node')
    c.require(len(base['nodes'])==3 and all(v['environment']==document['environment'] for v in base['nodes'])
        and all(e['runtime']['environment']==document['environment'] for e in base['entries']),
        'persistent reviewed environment scope differs')
    for node,proof in targets.items():
        bound=next(v for v in base['nodes'] if (v['environment'],v['node'])==(document['environment'],node))
        c.require(proof['binding']==bound and proof['success'] is True and proof['readonly'] is True
            and proof['filesystem_writes']==proof['compiler_invocations']==proof['external_downloads']==0
            and proof['catalog_sha256']==document['source_catalog_sha256'],'PVC full hash review binding/scope differs')
        identity={k:bound[k] for k in ('node_uid','pvc_uid','pv_uid')}
        entries=[e for e in base['entries'] if identity in e.get('bindings',[])]
        c.require(sorted(proof['indexes'],key=lambda v:v['index_sha256'])==sorted([{
            'index_sha256':e['index_sha256'],'entry_sha256':digest(e),'runtime_sha256':digest(e['runtime']),
            'action_count':next(p['action_count'] for p in proof['indexes'] if p['index_sha256']==e['index_sha256']),
            'native_manifests':e.get('native_manifests',{})} for e in entries],key=lambda v:v['index_sha256']),
            'PVC source/index/runtime manifest review differs')
        c.require(proof['verified_files']>0 and proof['verified_bytes']>0 and c.HEX.fullmatch(proof['payload_inventory_sha256']),
            'full payload not verified')
        for prior in report['copy_authority']['targets'].values():
            c.require(proof['verified_files']==prior['files'] and proof['verified_bytes']==prior['bytes']
                and sorted(v['index_sha256'] for v in proof['indexes'])==sorted(prior['indexes']),
                'revalidation no longer matches independently copied original payload')
    c.require(len({(p['verified_files'],p['verified_bytes'],p['payload_inventory_sha256'],p['tool_lock_sha256'],p['tool_archive_sha256'])
        for p in targets.values()})==1,'independently verified per-node inventories differ')
    return digest(document)


def compose(catalog,document):
    ident=validate(document);result=copy.deepcopy(catalog)
    base=document['base_catalog']
    c.require(all(b in result['nodes'] for b in base['nodes']) and all(e in result['entries'] for e in base['entries']),
        'persistent approval changed original catalog entries or volume bindings')
    c.require('persistent_pvc_authority' not in result,'persistent authority already materialized')
    result['persistent_pvc_authority']={'authorization_id':ident,'document':copy.deepcopy(document)}
    return result


class Authority:
    def __init__(self,catalog,api,revocation_read=None):
        self.binding=catalog['binding'];value=catalog['persistent_pvc_authority'];self.document=value['document']
        self.ident=validate(self.document)
        c.require(value['authorization_id']==self.ident,'persistent authorization ID changed')
        self.revoked=revocations(revocation_read or revocation_api);self.api=api
        contract=api(f'repos/matrixorigin/CI/actions/runs/{SCANNER_CONTRACT}')
        c.require(contract['id']==SCANNER_CONTRACT and contract['head_sha']==SCANNER_REVISION
            and contract['status']=='completed' and contract['conclusion']=='success'
            and contract['path']=='.github/workflows/pr-node-cache-contract.yaml'
            and contract['run_attempt']==1,'persistent full-scan contract authority changed')

    def covers(self,entry):
        return entry in self.document['base_catalog']['entries'] and entry['runtime']['environment']==self.document['environment']

    def permits(self,entry,current):
        if not self.covers(entry):return None
        bound=self.binding;document=self.document;env=document['environment']
        c.require(bound in document['base_catalog']['nodes'] and bound['environment']==env,'persistent volume identity changed')
        rows=document['review']['targets'][bound['node']]['indexes']
        c.require(any(v['index_sha256']==entry['index_sha256'] and v['entry_sha256']==digest(entry)
            and v['runtime_sha256']==digest(current) for v in rows),'persistent source/runtime/object descriptor differs')
        if self.ident in self.revoked['authorization_ids'] or entry['index_sha256'] in self.revoked['indexes'] or env in self.revoked['environments']:
            return False
        p=entry['producer'];run=self.api(f'repos/{p["repository"]}/actions/runs/{p["id"]}')
        c.require(entry['compile_only'] is True and p['repository']=='matrixorigin/matrixone'
            and p['event'] in ('push','workflow_dispatch'),'persistent producer no longer trusted')
        # The reviewed artifact descriptor remains provenance, NOT a live
        # transport dependency. This synthetic descriptor contains no expiry.
        legacy.validate_provenance(entry,run,{**entry['artifact'],'workflow_run':{'id':p['id'],'head_sha':p['head_sha']}})
        copy=document['review']['copy_authority']
        publication=self.api(f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{copy["run_id"]}')
        c.require(publication['id']==copy['run_id'] and publication['head_sha']==copy['gitops_sha'] and publication['status']=='completed'
            and publication['conclusion']=='success' and publication['run_attempt']==1
            and publication['event']=='workflow_dispatch' and publication['path']=='.github/workflows/cloudsigma-up.yaml'
            and publication['head_branch']=='codex/ruh-pvc-readiness'
            and publication['display_title']==f'up cs-component / {env} / node-cache-replicate'
            and publication['actor']['login']==publication['triggering_actor']['login']=='Ariznawlll',
            'persistent protected publication source changed')
        return True


def materialize(root,temp):
    import node_cache_pr_native_catalog as native
    root=Path(root);temp=c.safe_path(temp)
    native_path,proof=native.materialize(root,temp)
    source=c.read_json(native_path)
    result=compose(source,c.read_json(root/'node-cache-pvc-authorization.json'))
    target=temp/'cs-reviewed-persistent-catalog.json'
    c.require(not target.exists() and not (temp/'cs-node-cache').exists(),'persistent private materialization not fresh')
    c.atomic_json(target,result)
    return {'authorization_id':result['persistent_pvc_authority']['authorization_id'],
        'old_catalog_canonical_sha256':digest(source),'catalog_canonical_sha256':digest(result),
        'old_entries_and_bindings_unchanged':True,'shared_cache_writes':0,'original_native_composition':proof}


if __name__=='__main__':
    import os,sys
    c.require(sys.argv[1:]==['materialize'],'unknown persistent authority operation')
    print(c.canonical(materialize(Path(__file__).resolve().parents[1]/'config',Path(os.environ['RUNNER_TEMP']))).decode())
