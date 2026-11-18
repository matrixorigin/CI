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


if __name__=='__main__':unittest.main()
