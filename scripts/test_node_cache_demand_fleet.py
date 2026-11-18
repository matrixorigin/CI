import copy
import unittest

import node_cache_demand_fleet as d
import node_cache_publish_control as p


class DemandFleet(unittest.TestCase):
    def storage(self):
        return {'metadata':{'name':'directpv-min-io-ephemeral'},'provisioner':'directpv-min-io',
            'volumeBindingMode':'WaitForFirstConsumer','reclaimPolicy':'Delete',
            'parameters':{'directpv.min.io/tier':'ephemeral'}}
    def fixture(self,env='mty',number=0):
        uid='00000000-0000-4000-8000-000000000001';node=f'mo-standard-{number}'
        policy={'environment':env,'node':node,'node_uids':{node:uid},'name':f'cs-{env}-incremental-n{number}-v1'}
        binding={'environment':env,'node':node,'node_uid':uid,'pvc_uid':uid,'pv_uid':uid}
        old_name=f'cs-{env}-large-7c14g'
        original={'metadata':{'name':old_name,'uid':uid,'labels':{'app.kubernetes.io/version':'0.14.1'}},
          'spec':{'githubConfigUrl':'https://github.com/matrixorigin/matrixone',
                  'githubConfigSecret':old_name+'-auth','minRunners':0,'maxRunners':10,
                  'runnerScaleSetLabels':['cs-large-7c14g'],'listenerTemplate':{'spec':{}},
                  'template':{'spec':{'containers':[{'name':'runner','image':p.IMAGE,
                       'resources':{'requests':{'cpu':'7','memory':'12Gi'},'limits':{'cpu':'7','memory':'14Gi'}}}]}}}}
        return policy,binding,original

    def test_all_twelve_nodes_no_fixed_count_and_original_unchanged(self):
        names=set()
        for env in ('mty','jed','ruh','mnl2'):
            for number in (0,1,2):
                policy,binding,old=self.fixture(env,number);before=copy.deepcopy(old)
                result=d.pool(policy,binding,old,self.storage());spec=result['spec']
                self.assertNotIn('maxRunners',spec);self.assertNotIn('minRunners',spec)
                self.assertIn(d.COMMON_ROUTE,spec['runnerScaleSetLabels'])
                self.assertEqual(spec['template']['spec']['nodeSelector']['kubernetes.io/hostname'],policy['node'])
                self.assertEqual(spec['template']['spec']['volumes'][1]['persistentVolumeClaim'],
                                 {'claimName':policy['name'],'readOnly':True})
                self.assertTrue(spec['template']['spec']['containers'][0]['volumeMounts'][1]['readOnly'])
                work=spec['template']['spec']['volumes'][0]['ephemeral']['volumeClaimTemplate']
                self.assertEqual(work['spec']['resources']['requests']['storage'],'128Gi')
                self.assertNotIn('ownerReferences',work['metadata'])
                self.assertEqual(spec['template']['spec']['containers'][0]['volumeMounts'][-1]['mountPath'],'/tmp')
                self.assertFalse(spec['template']['spec']['automountServiceAccountToken'])
                self.assertEqual(spec['template']['spec']['containers'][0]['resources'],
                                 old['spec']['template']['spec']['containers'][0]['resources'])
                self.assertEqual(old,before);names.add(result['metadata']['name'])
        self.assertEqual(len(names),12)

    def test_wrong_binding_or_unapproved_node_and_old_route_refused(self):
        for fault in ('env','node','uid','route'):
            with self.subTest(fault=fault):
                policy,binding,old=self.fixture()
                if fault=='env':binding['environment']='ruh'
                if fault=='node':policy['node']='unapproved-node'
                if fault=='uid':binding['node_uid']='changed'
                if fault=='route':old['spec']['runnerScaleSetLabels'].append(d.COMMON_ROUTE)
                with self.assertRaises(ValueError):d.pool(policy,binding,old,self.storage())

    def test_private_disk_contract_cannot_be_omitted_or_incompatible(self):
        policy,binding,old=self.fixture()
        with self.assertRaises(TypeError):d.pool(policy,binding,old)
        storage=self.storage();storage['volumeBindingMode']='Immediate'
        with self.assertRaises(ValueError):d.pool(policy,binding,old,storage)


if __name__=='__main__':unittest.main()
