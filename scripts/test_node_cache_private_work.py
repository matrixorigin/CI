import copy
import unittest

import node_cache_demand_fleet as d
import node_cache_fleet_control as bounded
import node_cache_private_work as w
import test_node_cache_demand_fleet as f


class PrivateWork(unittest.TestCase):
    def fixture(self):
        policy,binding,original=f.DemandFleet().fixture()
        spec=bounded.pool(policy,binding,original)['spec']['template']['spec']
        sc={'metadata':{'name':w.STORAGE_CLASS},'provisioner':'directpv-min-io',
            'volumeBindingMode':'WaitForFirstConsumer','reclaimPolicy':'Delete',
            'parameters':{'directpv.min.io/tier':'ephemeral'}}
        return spec,sc

    def test_cache_unchanged_private_work_and_tmp_not_root(self):
        spec,sc=self.fixture();old=copy.deepcopy(spec);new=w.apply(spec,sc)
        self.assertEqual(spec,old);self.assertEqual(new['volumes'][1],old['volumes'][1])
        claim=new['volumes'][0]['ephemeral']['volumeClaimTemplate']
        self.assertEqual(claim['spec']['resources']['requests']['storage'],'128Gi')
        self.assertNotIn('ownerReferences',claim['metadata'])
        self.assertNotIn('maxRunners',new)
        self.assertEqual(new['containers'][0]['resources'],old['containers'][0]['resources'])
        self.assertEqual(new['containers'][0]['volumeMounts'][-1],{'name':'work','mountPath':'/tmp','subPath':'.runner-private-tmp'})
        self.assertFalse(new['automountServiceAccountToken'])
        self.assertEqual(new['initContainers'][0]['volumeMounts'],[{'name':'work','mountPath':'/work'}])

    def test_storage_contract_fail_closed(self):
        for key,value in [('provisioner','other'),('volumeBindingMode','Immediate'),('reclaimPolicy','Retain')]:
            spec,sc=self.fixture();sc[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):w.apply(spec,sc)
        spec,sc=self.fixture();sc['metadata']['deletionTimestamp']='2026-11-18T00:00:00Z'
        with self.assertRaises(ValueError):w.apply(spec,sc)

    def test_bounds_and_conflicting_write_path_refused(self):
        for size in (True,0,64,257):
            spec,sc=self.fixture()
            with self.subTest(size=size),self.assertRaises(ValueError):w.apply(spec,sc,size)
        spec,sc=self.fixture();spec['containers'][0]['env'].append({'name':'TMPDIR','value':'/ci-cache'})
        with self.assertRaises(ValueError):w.apply(spec,sc)
