import copy
import datetime as dt
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import incremental_cache as c
import node_cache_uniform_native_control as uniform
import node_cache_persistent_native_append as writer
from test_node_cache_pvc_authority import PersistentAuthority,git_file

ROOT=Path(__file__).resolve().parents[1]/'config'


class UniformPolicy(unittest.TestCase):
    def test_exact_three_environments_and_nodes_keep_original_publish_scope(self):
        for env in uniform.REQUESTS:
            request=c.read_json(ROOT/f'node-native-uniform-request-{env}.json')
            operation=uniform.UniformNativePolicy(request,ROOT)
            policy={'environment':env,'node_uids':{b['node']:b['node_uid'] for b in operation.catalog['nodes'] if b['environment']==env}}
            for node in operation.profiles:
                binding=next(b for b in operation.catalog['nodes'] if (b['environment'],b['node'])==(env,node))
                profile=request['profiles']['mo-standard-0']
                entry={'index_sha256':'a'*64,'runtime':profile['runtime'],'bindings':[],'native_only':True,'compile_only':True,
                    'source':request['source_sha'],'native_manifests':{profile['native_key']:'b'*64},'artifact':{'id':2},
                    'producer':{'id':1,'repository':'matrixorigin/matrixone','head_sha':uniform.PRODUCER_CALLER,
                        'head_branch':'weilu-runner-test','event':'workflow_dispatch','actor':'Ariznawlll','run_attempt':1,
                        'path':'.github/workflows/race-seed-canary.yaml','referenced_workflows':[{'sha':uniform.PRODUCER_CI,
                            'path':'matrixorigin/CI/.github/workflows/node-cache-uniform-native-refresh.yaml@'+uniform.PRODUCER_CI}]}}
                base=uniform.engine.core.approval(binding,uniform.cache.catalog_for_node(operation.catalog,binding)['entries'],entry)
                self.assertEqual(operation.scope(policy,operation.catalog,entry,base)[0],binding)
                data=operation.instructions(entry,base)
                self.assertIn('persistent-catalog.json',data);self.assertIn(operation.script,data)
                self.assertLess(sum(len(k.encode())+len(v.encode()) for k,v in data.items()),900000)
                for fault in ('source','producer','node','key','parent'):
                    e,b=copy.deepcopy((entry,base))
                    if fault=='source':e['source']='0'*40
                    elif fault=='producer':e['producer']['head_sha']='0'*40
                    elif fault=='node':b['binding']['node']='mo-standard-9'
                    elif fault=='key':e['native_manifests']={'0'*64:'b'*64}
                    elif fault=='parent':b['existing_entries'].pop()
                    with self.subTest(env=env,node=node,fault=fault),self.assertRaises(ValueError):operation.scope(policy,operation.catalog,e,b)
        self.assertEqual(uniform.engine.PROFILES.keys(),{'mo-standard-1','mo-standard-2'})


class PersistentParentWriter(PersistentAuthority):
    def request(self):
        catalog=self.catalog();binding=self.nodes[1];identity={k:v for k,v in binding.items() if k!='marker_sha256'}
        candidate=copy.deepcopy(self.entry);candidate.update(index_sha256='e'*64,bindings=[],expires_at='2099-01-01T00:00:00Z')
        base={'schema':1,'binding':identity,'marker_sha256':binding['marker_sha256'],'existing_entries':[self.entry],
            'candidate_index_sha256':candidate['index_sha256'],'candidate_runtime':candidate['runtime']}
        p=candidate['producer'];runpath=f'repos/{p["repository"]}/actions/runs/{p["id"]}'
        metadata={runpath:self.oldapi(runpath)}
        for ident in (uniform.pvc.SCANNER_CONTRACT,uniform.pvc.APPROVAL_ISSUER_CONTRACT):
            path=f'repos/matrixorigin/CI/actions/runs/{ident}';metadata[path]=self.api(path)
        apath=f'repos/{p["repository"]}/actions/artifacts/{candidate["artifact"]["id"]}'
        request={'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'binding':identity,
            'run':metadata[runpath],'artifact':self.oldapi(apath),'persistent_primary_metadata':metadata,
            'live_revocations_git_file':git_file(self.clear)}
        return catalog,base,candidate,request

    def test_expired_parent_still_requires_live_grant_but_new_candidate_never_bypasses_transport(self):
        values=self.request()
        with patch.dict(os.environ,{'CS_CACHE_ENV':'mnl2','ACTUAL_NODE':'mo-standard-1'}), \
                patch.object(writer.old.cpus,'checked_cpu',return_value='f'*64), \
                patch.object(writer.approval,'binding',return_value=values[1]['binding']):
            writer.authorized(*values)
            for fault in ('candidate_expired','producer_failed','revoked','missing_issuer','old_stamp','registry_corrupt'):
                catalog,base,entry,request=copy.deepcopy(values)
                if fault=='candidate_expired':request['artifact']['expired']=True
                elif fault=='producer_failed':request['run']['conclusion']='failure'
                elif fault=='revoked':request['live_revocations_git_file']=git_file({**self.clear,'authorization_ids':[self.ident]})
                elif fault=='missing_issuer':request['persistent_primary_metadata'].pop(f'repos/matrixorigin/CI/actions/runs/{uniform.pvc.APPROVAL_ISSUER_CONTRACT}')
                elif fault=='old_stamp':request['checked_at']='2000-01-01T00:00:00Z'
                elif fault=='registry_corrupt':request['live_revocations_git_file']['sha']='0'*40
                with self.subTest(fault=fault),self.assertRaises(ValueError):writer.authorized(catalog,base,entry,request)


if __name__=='__main__':unittest.main()
