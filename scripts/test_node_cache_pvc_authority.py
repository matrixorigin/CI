import base64
import copy
import datetime as dt
import hashlib
import unittest
import io
import urllib.error
from unittest.mock import patch
import incremental_cache as c
import node_compile_cache as cache
import node_cache_pvc_authority as pvc
from test_node_compile_cache import approval
from pathlib import Path


def git_file(value):
    raw=c.canonical(value)
    return {'type':'file','path':'config/node-cache-pvc-revocations.json','encoding':'base64',
        'content':base64.b64encode(raw).decode(),'sha':hashlib.sha1(('blob '+str(len(raw))+'\0').encode()+raw).hexdigest()}


class PersistentAuthority(unittest.TestCase):
    def setUp(self):
        self.runtime={'environment':'mnl2','cpu':{'fixture':'cpu'},'tools':{'go':'exact-tool'}}
        self.entry,self.oldapi=approval('d'*64,self.runtime);self.entry['expires_at']='2000-01-01T00:00:00Z'
        self.nodes=[{'environment':'mnl2','node':f'mo-standard-{i}','node_uid':f'node-{i}',
            'pvc_uid':f'pvc-{i}','pv_uid':f'pv-{i}','marker_sha256':str(i)*64} for i in range(3)]
        self.entry['bindings']=[{k:n[k] for k in ('node_uid','pvc_uid','pv_uid')} for n in self.nodes]
        base={'schema':2,'nodes':self.nodes,'entries':[self.entry]}
        targets={n['node']:{'success':True,'readonly':True,'filesystem_writes':0,'compiler_invocations':0,
            'external_downloads':0,'binding':n,'catalog_sha256':pvc.digest(base),
            'indexes':[{'index_sha256':self.entry['index_sha256'],'entry_sha256':pvc.digest(self.entry),
                'runtime_sha256':pvc.digest(self.runtime),'action_count':1,'native_manifests':{}}],
            'verified_files':3,'verified_bytes':200,'payload_inventory_sha256':'a'*64,
            'tool_lock_sha256':'b'*64,'tool_archive_sha256':'c'*64} for n in self.nodes}
        made=[{'kind':'ConfigMap','name':'own','uid':'cm'}]+[
            {'kind':'Pod','name':'own-'+str(i),'uid':'pod-'+str(i)} for i in range(3)]
        self.copy={'run_id':42,'gitops_sha':'e'*40,'targets':{n['node']:{'files':3,'bytes':200,
            'indexes':['d'*64]} for n in self.nodes[1:]}}
        report={'complete':True,'environment':'mnl2','revision':pvc.SCANNER_REVISION,'contract_run':pvc.SCANNER_CONTRACT,
            'cluster_cache_writes':0,'compiler_invocations':0,'external_downloads':0,'cache_recreated':False,
            'automatic_retry':False,'helper_pods_absent':True,'catalog_sha256':pvc.digest(base),
            'created':made,'deleted':made[1:],'targets':targets,'copy_authority':self.copy}
        self.document={'schema':1,'lifecycle':'exact-volume-runtime-content-until-revoked',
            'environment':'mnl2','base_catalog':base,'review':report,'source_catalog_sha256':pvc.digest(base)}
        self.ident=pvc.digest(self.document)
        self.canonical=patch.object(pvc,'APPROVAL_SHA256',self.ident);self.canonical.start();self.addCleanup(self.canonical.stop)
        self.clear={'schema':1,'authorization_ids':[],'indexes':[],'environments':[]}
        self.registry=patch.object(pvc,'revocation_api',return_value=git_file(self.clear));self.read=self.registry.start();self.addCleanup(self.registry.stop)
        self.calls=[]

    def api(self,path):
        self.calls.append(path)
        self.assertIn('/runs/',path);self.assertNotIn('/artifacts/',path)
        if path.endswith('/'+str(pvc.SCANNER_CONTRACT)):
            return {'id':pvc.SCANNER_CONTRACT,'head_sha':pvc.SCANNER_REVISION,'status':'completed',
                'conclusion':'success','path':'.github/workflows/pr-node-cache-contract.yaml','run_attempt':1}
        if path.endswith('/'+str(pvc.APPROVAL_ISSUER_CONTRACT)):
            return {'id':pvc.APPROVAL_ISSUER_CONTRACT,'head_sha':pvc.APPROVAL_ISSUER_REVISION,'status':'completed',
                'conclusion':'success','path':'.github/workflows/pr-node-cache-contract.yaml','run_attempt':1}
        self.assertNotIn('cloudsigma-gitops',path,'ordinary consumer must not require private origin access')
        return self.oldapi(path)

    def catalog(self):return pvc.compose(self.document['base_catalog'],self.document)
    def selected(self,**kwargs):
        bound=cache.catalog_for_node(self.catalog(),self.nodes[1])
        return cache.reviewed_entries(bound,self.runtime,self.api,**kwargs)

    def test_artifact_deleted_or_expired_and_old_dates_do_not_expire_independently_reviewed_pvc(self):
        old=copy.deepcopy(self.entry)
        self.assertEqual(self.selected(now=dt.datetime(2099,1,1,tzinfo=dt.timezone.utc)),[self.entry])
        self.assertEqual(self.entry,old);self.assertTrue(self.calls)
        status={};self.selected(persistent_status=status)
        self.assertEqual(status['authorization_id'],self.ident);self.assertEqual(status['authorized_indexes'],['d'*64])

    def test_unreviewed_expired_entries_keep_original_expiry_rule(self):
        self.assertEqual(cache.reviewed_entries({'entries':[self.entry]},self.runtime,self.api),[])

    def test_live_revoke_id_index_or_environment_never_falls_back_to_old_authority(self):
        for key,value in (('authorization_ids',self.ident),('indexes','d'*64),('environments','mnl2')):
            denied={**self.clear,key:[value]};self.read.return_value=git_file(denied)
            with self.subTest(key=key):self.assertEqual(self.selected(),[])

    def test_revocation_unavailable_is_not_a_bypass_and_blob_corruption_fails(self):
        self.read.side_effect=PermissionError('unavailable')
        self.assertEqual(self.selected(),[])
        self.read.side_effect=None;bad=git_file(self.clear);bad['sha']='0'*40;self.read.return_value=bad
        with self.assertRaises(ValueError):self.selected()

    def test_runtime_or_volume_changes_do_not_inherit_persistent_trust(self):
        bound=cache.catalog_for_node(self.catalog(),self.nodes[1])
        self.assertEqual(cache.reviewed_entries(bound,{**self.runtime,'tools':{'go':'changed'}},self.api),[])
        bound['binding']={**bound['binding'],'pvc_uid':'replacement'}
        with self.assertRaises(ValueError):cache.reviewed_entries(bound,self.runtime,self.api)

    def test_failed_or_changed_producer_and_scan_contract_are_rejected(self):
        for fault in ('producer','issuer','contract'):
            def bad(path):
                value=self.api(path)
                if (fault=='producer' and path.endswith('/1')) or (fault=='issuer' and path.endswith('/'+str(pvc.APPROVAL_ISSUER_CONTRACT))) or (fault=='contract' and path.endswith('/'+str(pvc.SCANNER_CONTRACT))):
                    value['conclusion']='failure'
                return value
            with self.subTest(fault=fault),self.assertRaises(ValueError):
                cache.reviewed_entries(cache.catalog_for_node(self.catalog(),self.nodes[1]),self.runtime,bad)

    def test_partial_self_approved_wrong_hash_or_changed_prior_payload_cannot_be_composed(self):
        for fault in ('hash','incomplete','write','retirement','node','payload','entry'):
            value=copy.deepcopy(self.document);r=value['review']
            if fault=='incomplete':r['complete']=False
            elif fault=='write':r['cluster_cache_writes']=1
            elif fault=='retirement':r['deleted']=[]
            elif fault=='node':r['targets'].pop('mo-standard-2')
            elif fault=='payload':r['targets']['mo-standard-0']['verified_bytes']+=1
            elif fault=='entry':r['targets']['mo-standard-1']['indexes'][0]['entry_sha256']='f'*64
            with patch.object(pvc,'APPROVAL_SHA256',self.ident if fault=='hash' else pvc.digest(value)):
                if fault=='hash':value['lifecycle']='other'
                with self.subTest(fault=fault),self.assertRaises(ValueError):pvc.compose(value['base_catalog'],value)


class ActualIndependentReview(unittest.TestCase):
    def test_committed_three_volume_review_preserves_original_expiry_and_content_identity(self):
        root=Path(__file__).resolve().parents[1]/'config'
        doc=c.read_json(root/'node-cache-pvc-authorization.json')
        self.assertEqual(pvc.validate(doc),pvc.APPROVAL_SHA256)
        base=c.read_json(root/'node-compile-cache-catalog.json')
        published=pvc.compose(base,doc)
        self.assertEqual(published['entries'],base['entries']);self.assertEqual(published['nodes'],base['nodes'])
        entry=next(e for e in base['entries'] if e['index_sha256']=='40e661e10777fbb7a93bf03ca28624801f6445b2ce2f21b9db22e0e899db06a0')
        self.assertEqual(entry['expires_at'],'2026-11-18T13:47:20+00:00')
        self.assertEqual([p['verified_files'] for p in doc['review']['targets'].values()],[14296]*3)
        self.assertEqual([p['verified_bytes'] for p in doc['review']['targets'].values()],[9145029638]*3)

    def test_four_environment_candidates_preserve_every_bound_generation_and_are_not_self_activating(self):
        import node_cache_pr_native_catalog as native
        root=Path(__file__).resolve().parents[1]/'config'
        original,_=native.load(root)
        hashes={'mty':'2b9d0e701ce12b42bac6088734ff0ffcb152966c4ae95a99f7795e5ddafb1cb0',
            'jed':'53b0ebf81cc99228514b4d76e6454f80d1f5795518f1d7359c369bdff32c8d1c',
            'ruh':'d5536565bdbe3ad5535c43fc4bb27a1e2538e9a54ce3e7fb33597234efff23ce'}
        for env,expected in hashes.items():
            doc=c.read_json(root/f'node-cache-pvc-authorization-{env}.json')
            self.assertEqual(pvc.validate_review(doc),expected)
            self.assertEqual(doc['base_catalog']['nodes'],[n for n in original['nodes'] if n['environment']==env])
            self.assertEqual(doc['base_catalog']['entries'],[e for e in original['entries'] if e['runtime']['environment']==env])
            with self.assertRaises(ValueError):pvc.compose(original,doc)
            self.assertEqual(len(doc['review']['targets']),3)

    def test_mty_composite_cannot_omit_cpu_producer_or_base_payload_or_change_tools(self):
        root=Path(__file__).resolve().parents[1]/'config'
        doc=c.read_json(root/'node-cache-pvc-authorization-mty.json')
        for fault in ('cpu_producer','base','tool','inventory','binding'):
            value=copy.deepcopy(doc)
            if fault=='cpu_producer':value['all_source_producers'].pop()
            elif fault=='base':value['original_base_authorization']['review']['complete']=False
            elif fault=='tool':value['review']['targets']['mo-standard-1']['tool_archive_sha256']='a'*64
            elif fault=='inventory':value['review']['targets']['mo-standard-1']['indexes'].pop()
            elif fault=='binding':value['review']['targets']['mo-standard-1']['binding']['pvc_uid']='other'
            with self.subTest(fault=fault),self.assertRaises(ValueError):pvc.validate_review(value)




if __name__=='__main__':unittest.main()
