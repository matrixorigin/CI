import copy
import json
import unittest
from unittest.mock import patch

import node_cache_workspace_probe_control as q
import node_cache_demand_plan as plans
import test_node_cache_demand_plan as plan_fixture
import test_node_cache_publish_control as resource_fixture


class ProbeChecks(unittest.TestCase):
    def test_catalog_copy_and_producer_authorities_fail_closed(self):
        cat,_,_,_=plan_fixture.AllNodePlan().fixture()
        lock={'producer':{'repository':'matrixorigin/CI','id':7},'artifact':{'id':8}}
        run={'id':17,'head_sha':'a'*40,'conclusion':'success','status':'completed','event':'workflow_dispatch',
            'run_attempt':1,'head_branch':'codex/ruh-pvc-readiness','path':'.github/workflows/cloudsigma-up.yaml',
            'actor':{'login':'Ariznawlll'},'display_title':'up cs-component / mty / node-cache-replicate'}
        artifact={'digest':'sha256:'+'b'*64,'expired':False,'workflow_run':{'id':17,'head_sha':'a'*40}}
        evidence={'schema':1,'catalog_canonical_sha256':q.base.replication.sha(q.c.canonical(cat)),
            'copies':[{'environment':'mty','run_id':17,'gitops_sha':'a'*40,'artifact_id':99,'artifact_digest':artifact['digest']}]}
        for fault in (None,'digest','branch','actor','expired','producer'):
            local=copy.deepcopy(evidence);run_value=copy.deepcopy(run);artifact_value=copy.deepcopy(artifact)
            if fault=='digest':local['catalog_canonical_sha256']='0'*64
            if fault=='branch':run_value['head_branch']='main'
            if fault=='actor':run_value['actor']['login']='other'
            if fault=='expired':artifact_value['expired']=True
            def api(path):return run_value if path.endswith('/runs/17') else artifact_value if path.endswith('/artifacts/99') else {}
            with patch.object(q.base.n,'reviewed_entries',side_effect=lambda selected,*args:[] if fault=='producer' else selected['entries']), \
                 patch.object(q.base.tools,'authorize'):
                if fault:
                    with self.subTest(fault=fault),self.assertRaises(ValueError):q.authenticate(cat,lock,local,'mty',api)
                else:q.authenticate(cat,lock,local,'mty',api)
    def test_probe_not_registered_runner_cache_readonly_private_work_unchanged(self):
        cat,policy,old,sc=plan_fixture.AllNodePlan().fixture()
        fleet=plans.plan(cat,policy,old,sc,work_size_gib=q.WORK_GIB)[0];before=copy.deepcopy(fleet)
        pod=q.probe_pod(fleet,'new-probe','new-probe-scripts');spec=pod['spec']
        self.assertEqual(fleet,before);self.assertFalse(spec['automountServiceAccountToken'])
        self.assertNotIn('serviceAccountName',spec)
        self.assertEqual(spec['containers'][0]['command'],['/bin/bash','-euc','sleep 3500'])
        self.assertEqual(spec['containers'][0]['resources'],q.PROBE_RESOURCES)
        self.assertTrue(spec['volumes'][1]['persistentVolumeClaim']['readOnly'])
        self.assertTrue(spec['containers'][0]['volumeMounts'][1]['readOnly'])
        self.assertIn('ephemeral',spec['volumes'][0]);self.assertEqual(spec['restartPolicy'],'Never')

    def test_ready_pod_template_uid_and_privilege_boundaries(self):
        cat,policy,old,sc=plan_fixture.AllNodePlan().fixture()
        expected=q.probe_pod(plans.plan(cat,policy,old,sc,work_size_gib=q.WORK_GIB)[0],'new-probe','scripts')
        for fault in (None,'uid','privileged','capability','host','restart'):
            actual=copy.deepcopy(expected);actual['metadata']['uid']='own-pod'
            actual['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
            if fault=='uid':actual['metadata']['uid']='replacement'
            if fault=='privileged':actual['spec']['containers'][0]['securityContext']['privileged']=True
            if fault=='capability':actual['spec']['containers'][0]['securityContext']['capabilities']['add']=['SYS_ADMIN']
            if fault=='host':actual['spec']['hostPID']=True
            if fault=='restart':actual['status']['containerStatuses']=[{'restartCount':1}]
            if fault:
                with self.subTest(fault=fault),self.assertRaises(ValueError):q.checked_pod(expected,actual,'own-pod')
            else:self.assertTrue(q.checked_pod(expected,actual,'own-pod'))

    def snapshot(self):
        policy,snap=resource_fixture.fixture()
        snap['sc'].update(metadata={'name':'directpv-min-io-ephemeral','uid':'class-uid'},reclaimPolicy='Delete')
        snap['quotas']={'items':[]};snap['drives']['items'][0]['status']['freeCapacity']=600*q.c.GIB
        return policy,snap

    def test_two_private_claims_admitted_with_real_storage_reserve(self):
        policy,snap=self.snapshot();result=q.admission(policy,snap,'mo-standard-0',new_claims=2,new_pods=2)
        self.assertEqual(result['planned_private_claims'],2);self.assertEqual(result['storage_class_uid'],'class-uid')

    def test_existing_workspaces_keep_twenty_gib_but_new_claims_need_full_reservation(self):
        policy,snap=self.snapshot()
        snap['drives']['items'][0]['status']['freeCapacity']=24*q.c.GIB
        q.admission(policy,snap,'mo-standard-0')
        with self.assertRaises(ValueError):q.admission(policy,snap,'mo-standard-0',new_claims=1,new_pods=1)
        # The old copy default remains 64Gi, and even retirement may not bypass20Gi.
        with self.assertRaises(ValueError):q.base.snapshot_guard(policy,snap,'mo-standard-0')
        snap['drives']['items'][0]['status']['freeCapacity']=19*q.c.GIB
        with self.assertRaises(ValueError):q.admission(policy,snap,'mo-standard-0')

    def test_space_quota_actual_pressure_and_terminating_class_refused(self):
        for fault in ('space','quota','actual-memory','class','identity'):
            policy,snap=self.snapshot()
            if fault=='space':snap['drives']['items'][0]['status']['freeCapacity']=(2*q.WORK_GIB+19)*q.c.GIB
            if fault=='quota':snap['quotas']['items']=[{'status':{'hard':{'persistentvolumeclaims':'2'},'used':{'persistentvolumeclaims':'1'}}}]
            if fault=='actual-memory':snap['stats']['node']['memory']['availableBytes']=5*q.c.GIB
            if fault=='class':snap['sc']['metadata']['deletionTimestamp']='now'
            if fault=='identity':policy['node_uids']['mo-standard-0']='replaced'
            with self.subTest(fault=fault),self.assertRaises(ValueError):q.admission(policy,snap,'mo-standard-0',new_claims=2,new_pods=2)

    def volume(self):
        pod={'metadata':{'name':'probe','uid':'pod-uid'},'spec':{'nodeName':'mo-standard-0'}}
        pvc={'metadata':{'name':'probe-work','namespace':'cicd','uid':'work-pvc','ownerReferences':[
            {'apiVersion':'v1','kind':'Pod','name':'probe','uid':'pod-uid','controller':True}]},
            'spec':{'volumeName':'work-pv','storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce'],
                'resources':{'requests':{'storage':str(q.WORK_GIB)+'Gi'}}},'status':{'phase':'Bound','capacity':{'storage':str(q.WORK_GIB)+'Gi'}}}
        pv={'metadata':{'name':'work-pv','uid':'work-pv-uid'},'status':{'phase':'Bound'},'spec':{
            'claimRef':{'name':'probe-work','namespace':'cicd','uid':'work-pvc'},'csi':{'driver':'directpv-min-io'},
            'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce'],'capacity':{'storage':str(q.WORK_GIB)+'Gi'},
            'persistentVolumeReclaimPolicy':'Delete',
            'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{
                'key':'directpv.min.io/node','operator':'In','values':['mo-standard-0']}]}]}}}}
        return pod,pvc,pv,{'pvc_uid':'cache-pvc','pv_uid':'cache-pv'}

    def test_private_volume_owner_node_bound_and_cache_separation(self):
        pod,pvc,pv,cache=self.volume();result=q.private_volume(pod,pvc,pv,'mo-standard-0',cache)
        self.assertEqual(result['pod_uid'],'pod-uid');self.assertEqual(result['bytes'],q.WORK_GIB*q.c.GIB)
        for fault in ('owner','cache','node','binding','size'):
            pod,pvc,pv,cache=self.volume()
            if fault=='owner':pvc['metadata']['ownerReferences'][0]['uid']='other-pod'
            if fault=='cache':pvc['metadata']['uid']='cache-pvc'
            if fault=='node':pod['spec']['nodeName']='mo-standard-1'
            if fault=='binding':pv['spec']['claimRef']['uid']='replaced'
            if fault=='size':pv['spec']['capacity']['storage']='64Gi'
            with self.subTest(fault=fault),self.assertRaises(ValueError):q.private_volume(pod,pvc,pv,'mo-standard-0',cache)


if __name__=='__main__':unittest.main()
