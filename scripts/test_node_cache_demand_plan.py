import copy
import unittest

import node_cache_demand_plan as p
import node_cache_replication_catalog as catalogs
import test_node_cache_replication_catalog as catalog_fixture
import test_node_cache_demand_fleet as fleet_fixture


class AllNodePlan(unittest.TestCase):
    def fixture(self,env='mty'):
        catalog,receipts=catalog_fixture.Proposal().fixture();catalog=catalogs.propose(catalog,receipts)
        policy,_,original=fleet_fixture.DemandFleet().fixture(env)
        policy['node_uids']={n['node']:n['node_uid'] for n in catalog['nodes'] if n['environment']==env}
        return catalog,policy,original,fleet_fixture.DemandFleet().storage()

    def test_all_twelve_no_hidden_runner_cap_and_only_private_writes(self):
        names=set()
        for env in p.ENVIRONMENTS:
            cat,policy,old,sc=self.fixture(env);before=copy.deepcopy((cat,policy,old,sc))
            resources=p.plan(cat,policy,old,sc)
            self.assertEqual(len(resources),3)
            for resource in resources:
                spec=resource['spec'];pod=spec['template']['spec'];names.add(resource['metadata']['name'])
                self.assertNotIn('minRunners',spec);self.assertNotIn('maxRunners',spec)
                self.assertEqual(pod['volumes'][0]['ephemeral']['volumeClaimTemplate']['spec']['resources']['requests']['storage'],'128Gi')
                self.assertTrue(pod['volumes'][1]['persistentVolumeClaim']['readOnly'])
            self.assertEqual((cat,policy,old,sc),before)
        self.assertEqual(len(names),12)

    def test_missing_node_replaced_identity_and_shared_volume_refused(self):
        for fault in ('missing','node','volume','entries'):
            cat,policy,old,sc=self.fixture()
            if fault=='missing':cat['nodes'].pop()
            if fault=='node':policy['node_uids']['mo-standard-1']='changed'
            if fault=='volume':cat['nodes'][1]['pvc_uid']=cat['nodes'][0]['pvc_uid']
            if fault=='entries':cat['entries']=[]
            with self.subTest(fault=fault),self.assertRaises(ValueError):p.plan(cat,policy,old,sc)

    def test_explicit_node0_node1_rollout_preserves_node2_target_without_count_cap(self):
        cat,policy,old,sc=self.fixture()
        by_node={n:next(e['runtime']['cpu'] for e in p.cache.catalog_for_node(cat,
            next(b for b in cat['nodes'] if (b['environment'],b['node'])==('mty',n)))['entries']) for n in p.NODES}
        for e in cat['entries']:
            if e['runtime']['environment']=='mty':e['native_manifests']={'a'*64:'b'*64}
        before=copy.deepcopy((cat,policy,old,sc))
        result=p.staged_plan(cat,policy,old,sc,['mo-standard-0','mo-standard-1'],by_node)
        self.assertEqual(result['pending_nodes'],['mo-standard-2'])
        self.assertEqual(result['target_nodes'],list(p.NODES))
        self.assertEqual(len(result['resources']),2)
        self.assertIsNone(result['runner_count_cap'])
        self.assertFalse(result['live_capacity_accepted'])
        for resource in result['resources']:
            self.assertNotIn('maxRunners',resource['spec']);self.assertNotIn('minRunners',resource['spec'])
        self.assertEqual((cat,policy,old,sc),before)

    def test_staged_rollout_cannot_route_unpublished_or_mismatched_cpu(self):
        cat,policy,old,sc=self.fixture()
        by_node={n:{} for n in p.NODES}
        for nodes in ([],['mo-standard-0','mo-standard-0'],['other'],['mo-standard-1']):
            with self.subTest(nodes=nodes),self.assertRaises(ValueError):
                p.staged_plan(cat,policy,old,sc,nodes,by_node)
        with self.assertRaises(ValueError):p.staged_plan(cat,policy,old,sc,['mo-standard-0'],{})


if __name__=='__main__':unittest.main()
