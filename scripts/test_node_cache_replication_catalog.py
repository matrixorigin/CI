import copy
import hashlib
import json
from pathlib import Path
import unittest

import node_cache_replication_catalog as p


class Proposal(unittest.TestCase):
    def fixture(self):
        catalog=json.loads((Path(__file__).resolve().parents[1]/'config/node-compile-cache-catalog.json').read_text())
        receipts={}
        for ordinal,env in enumerate(('mty','jed','ruh','mnl2'),1):
            expected=sorted(e['index_sha256'] for e in catalog['entries'] if e['runtime']['environment']==env)
            receipt={'environment':env,'complete':True,'helper_pods_absent':True,
                'provenance_verified_before_write':True,'compiler_invocations':0,'created':[],'deleted':[],'targets':{}}
            for number in (1,2):
                node=f'mo-standard-{number}';claim=f'cs-{env}-incremental-n{number}-v1'
                identity={'environment':env,'node':node,**{key:f'{i}0000000-0000-4000-8000-{ordinal*10+number:012d}'
                           for i,key in enumerate(('node_uid','pvc_uid','pv_uid'),1)}}
                marker=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                proof={'success':True,'independently_verified':True,'compiler_invocations':0,'external_downloads':0,
                       'indexes':expected,'binding':{**identity,'marker_sha256':marker}}
                receipt['targets'][node]={'claim':claim,'binding':identity,'received':copy.deepcopy(proof),'verify':proof}
                receipt['created'].append({'kind':'PersistentVolumeClaim','name':claim,'uid':identity['pvc_uid']})
            receipts[env]=receipt
        return catalog,receipts

    def test_all_twelve_nodes_without_changing_reviewed_compilation_bytes(self):
        catalog,receipts=self.fixture();before=copy.deepcopy(catalog)
        result=p.propose(catalog,receipts)
        self.assertEqual(len(result['nodes']),12);self.assertEqual(catalog,before)
        for original,updated in zip(catalog['entries'],result['entries']):
            self.assertEqual({k:v for k,v in original.items() if k!='bindings'},
                             {k:v for k,v in updated.items() if k!='bindings'})
            self.assertEqual(len(updated['bindings']),3)
        self.assertEqual(len(result['native_authorities']),12)

    def test_existing_nodes_or_unverified_or_wrong_objects_rejected(self):
        for fault in ('failed','cleanup','checksum','indexes','uid','repeat'):
            with self.subTest(fault=fault):
                catalog,receipts=self.fixture();row=receipts['mty']['targets']['mo-standard-1']
                if fault=='failed':receipts['mty']['complete']=False
                if fault=='cleanup':receipts['mty']['helper_pods_absent']=False
                if fault=='checksum':row['verify']['binding']['marker_sha256']='0'*64
                if fault=='indexes':row['verify']['indexes']=[]
                if fault=='uid':receipts['mty']['created'][0]['uid']='different'
                if fault=='repeat':catalog=p.propose(catalog,receipts)
                with self.assertRaises(AssertionError):p.propose(catalog,receipts)

    def test_recovery_adopts_existing_claims_without_replacing_them(self):
        catalog,receipts=self.fixture();row=receipts['jed']
        first,second=copy.deepcopy(row['created'])
        prior={'environment':'jed','compiler_invocations':0,'provenance_verified_before_write':True,
            'created':[second],'adopted_resources':[first]}
        row.update(created=[],deleted=[],adopted_resources=copy.deepcopy(prior['created']),resumed_chunk_run_id=17)
        result=p.propose(catalog,receipts,{17:prior})
        self.assertEqual(len(result['nodes']),12)
        self.assertEqual(next(n['pvc_uid'] for n in result['nodes'] if n['environment']=='jed' and n['node']=='mo-standard-1'),first['uid'])
        for fault in ('missing','replacement','cross-environment','adoption'):
            bad=copy.deepcopy(prior);current=copy.deepcopy(receipts)
            if fault=='replacement':bad['adopted_resources'][0]['uid']='changed'
            if fault=='cross-environment':bad['environment']='ruh'
            if fault=='adoption':current['jed']['adopted_resources']=[]
            with self.subTest(fault=fault),self.assertRaises(AssertionError):
                p.propose(catalog,current,{} if fault=='missing' else {17:bad})

    def test_direct_adoption_is_validated_not_assumed(self):
        catalog,receipts=self.fixture();row=receipts['mnl2']
        row['adopted_resources']=[row['created'].pop(0)]
        self.assertEqual(len(p.propose(catalog,receipts)['nodes']),12)
        row['adopted_resources'][0]['uid']='changed'
        with self.assertRaises(AssertionError):p.propose(catalog,receipts)


if __name__=='__main__':unittest.main()
