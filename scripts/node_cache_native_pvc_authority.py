"""Use authenticated native supplements without distributing private cluster reports."""
from pathlib import Path
import incremental_cache as c
import node_cache_pvc_authority as parent
import node_compile_cache as cache

CANDIDATES={
 'mnl2':'95eb7f15a789cee0a5883df6fd731501b04ed7cda7d56a36df87c6c67773ce09',
 'jed':'553451101bb5684be220af6db6a7eb23688b43c59ee4c0534b5d5cae95f711bc',
 'ruh':'a30e15839dd4db62ec8aa7e9496d9ae73dc787066002f6285c29364a3abc57ac'}
ISSUER=None  # candidate contract cannot authorize itself

def document(config,env):
    cfg=Path(config)
    sup=c.read_json(cfg/f'node-native-reviewed-supplement-{env}.json')
    c.require(parent.digest(sup)==CANDIDATES[env] and sup['environment']==env,'native supplement differs from independent review')
    prior=c.read_json(cfg/('node-cache-pvc-authorization.json' if env=='mnl2' else f'node-cache-pvc-authorization-{env}.json'))
    c.require(parent.validate(prior)==sup['parent_sha256'] and prior['environment']==env,'native parent differs')
    base=prior['base_catalog'];entry=sup['entry'];receipts=sup['receipts']
    bindings=[{k:b[k] for k in ('node_uid','pvc_uid','pv_uid')} for b in base['nodes']]
    c.require(bindings==entry['bindings'] and len(bindings)==3 and entry['runtime']['environment']==env
              and entry['native_only'] is True and entry['compile_only'] is True
              and entry['producer']['repository']=='matrixorigin/matrixone' and len(entry['native_manifests'])==1,
              'wrong source, volume or runtime')
    c.require({r['node'] for r in receipts}=={b['node'] for b in base['nodes']} and len(receipts)==3
              and len({r['raw_sha256'] for r in receipts})==3 and all(c.HEX.fullmatch(r['raw_sha256'])
              and r['objects']==45 and r['base_transmitted']==0 for r in receipts),'native protected receipt coverage differs')
    return {'schema':3,'environment':env,'parent_authorization':prior,'parent_authorization_id':sup['parent_sha256'],
            'base_catalog':{**base,'entries':base['entries']+[entry]},'additional_entry':entry,
            'supplement_sha256':parent.digest(sup)}

def validate(doc,cfg):
    c.require(ISSUER is not None and doc==document(cfg,doc['environment']),'native candidate not activated or differs')
    return parent.digest(doc)

class Authority:
    def __init__(self,local,api,revocation_read=None):
        value=local['persistent_pvc_authority']; self.doc=value['document']; self.binding=local['binding']
        self.ident=validate(self.doc,Path(__file__).resolve().parents[1]/'config')
        c.require(value['authorization_id']==self.ident and self.binding in self.doc['base_catalog']['nodes'],'wrong native volume')
        prior=self.doc['parent_authorization']
        old={'binding':self.binding,'entries':cache.catalog_for_node(prior['base_catalog'],self.binding)['entries'],
             'persistent_pvc_authority':{'authorization_id':self.doc['parent_authorization_id'],'document':prior}}
        self.parent=parent.Authority(old,api,revocation_read); self.api=api; self.revoked=self.parent.revoked
        revision,ident=ISSUER
        run=api(f'repos/matrixorigin/CI/actions/runs/{ident}')
        c.require(run['id']==ident and run['head_sha']==revision and run['run_attempt']==1
                  and run['status']=='completed' and run['conclusion']=='success' and run['event']=='push'
                  and run['head_branch']=='codex/pr-node-cache-two-group-capacity'
                  and run['path']=='.github/workflows/pr-node-cache-contract.yaml','independent native issuer differs')
        self.publication_metadata_checks=self.parent.publication_metadata_checks+[{'issuer_repository':'matrixorigin/CI',
            'issuer_run':ident,'http_status':200,'protected_origin_verified_at_independent_approval':True}]

    def covers(self,entry):
        return entry in self.doc['base_catalog']['entries'] and entry['runtime']['environment']==self.doc['environment']

    def permits(self,entry,current):
        if not self.covers(entry):return None
        if ({self.ident,self.parent.ident}&set(self.revoked['authorization_ids']) or
                entry['index_sha256'] in self.revoked['indexes'] or self.doc['environment'] in self.revoked['environments']):return False
        if self.parent.covers(entry):return self.parent.permits(entry,current)
        c.require(entry==self.doc['additional_entry'] and entry['runtime']==current,'native runtime/generation changed')
        p=entry['producer'];run=self.api(f'repos/{p["repository"]}/actions/runs/{p["id"]}')
        cache.legacy.validate_provenance(entry,run,{**entry['artifact'],'workflow_run':{'id':p['id'],'head_sha':p['head_sha']}})
        return True
