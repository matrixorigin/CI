import copy
import datetime as dt
import unittest
from unittest import mock
import node_cache_publish_control as p

U='00000000-0000-0000-0000-000000000001'
NOW=dt.datetime.now(dt.timezone.utc)


def fixture():
    policy={'environment':'mty','node':'mo-standard-0','name':'cs-mty-incremental-n0-v1',
            'namespace_uids':{'cicd':U,'mocservices-mty':U,'kube-system':U},
            'node_uids':{'mo-standard-0':U},'workloads':[]}
    node={'metadata':{'name':'mo-standard-0','uid':U,'labels':{
        'directpv.min.io/node':'mo-standard-0','directpv.min.io/tier':'ephemeral'}},'spec':{},
        'status':{'conditions':[{'type':k,'status':v} for k,v in {
           'Ready':'True','MemoryPressure':'False','DiskPressure':'False','PIDPressure':'False'}.items()],
           'allocatable':{'cpu':'32','memory':'128Gi'}}}
    snapshot={'namespaces':{'items':[{'metadata':{'name':k,'uid':v}} for k,v in policy['namespace_uids'].items()]},
      'nodes':{'items':[node]},'sc':{'provisioner':'directpv-min-io','volumeBindingMode':'WaitForFirstConsumer',
                                   'parameters':{'directpv.min.io/tier':'ephemeral'}},
      'stats':{'node':{'cpu':{'time':NOW.isoformat(),'usageNanoCores':1e8},
                       'memory':{'time':NOW.isoformat(),'availableBytes':100*p.c.GIB},
                       'fs':{'time':NOW.isoformat(),'availableBytes':80*p.c.GIB}}},
      'pods':{'items':[]},'drives':{'items':[{'metadata':{'labels':node['metadata']['labels']},
          'status':{'status':'Ready','freeCapacity':200*p.c.GIB}}]}}
    return policy,snapshot


class ControlTest(unittest.TestCase):
    def test_fresh_identity_capacity(self):
        policy,s=fixture();self.assertTrue(p.guard(policy,s,at=NOW)['existing_workloads_unchanged'])

    def test_negative_guards(self):
        for field in ('namespace','node','pressure','stale','root','memory','cpu','drive','runner'):
            with self.subTest(field=field):
                policy,s=fixture()
                if field=='namespace':s['namespaces']['items'][0]['metadata']['uid']='changed'
                if field=='node':s['nodes']['items'][0]['metadata']['uid']='changed'
                if field=='pressure':s['nodes']['items'][0]['status']['conditions'][1]['status']='True'
                if field=='stale':s['stats']['node']['cpu']['time']=(NOW-dt.timedelta(seconds=121)).isoformat()
                if field=='root':s['stats']['node']['fs']['availableBytes']=59*p.c.GIB
                if field=='memory':s['stats']['node']['memory']['availableBytes']=31*p.c.GIB
                if field=='cpu':s['stats']['node']['cpu']['usageNanoCores']=6e9
                if field=='drive':s['drives']['items'][0]['status']['freeCapacity']=127*p.c.GIB
                if field=='runner':s['pods']['items']=[{'metadata':{'ownerReferences':[{'kind':'EphemeralRunner'}]},'status':{'phase':'Pending'}}]
                with self.assertRaises(ValueError):p.guard(policy,s,at=NOW)

    def test_existing_workload_restarts_and_limits(self):
        policy,s=fixture()
        pod={'metadata':{'name':'cn','namespace':'business','ownerReferences':[{'kind':'CloneSet'}]},
             'spec':{'nodeName':policy['node'],'containers':[{'resources':{'limits':{'cpu':'4','memory':'16Gi'}}}]},
             'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}],
                       'containerStatuses':[{'restartCount':2}]}}
        policy['workloads']=[p.workload(pod)];s['pods']['items']=[pod]
        p.guard(policy,s,at=NOW)
        pod['spec']['initContainers']=[{'name':'init','resources':{'limits':{'cpu':'1'}}}]
        pod['status']['initContainerStatuses']=[{'name':'init','restartCount':1,'state':{'terminated':{'exitCode':0}}}]
        initial=p.guard(policy,s,at=NOW)
        policy['initial_init_restarts']=initial['init_restarts']
        pod['status']['initContainerStatuses'][0]['restartCount']=2
        with self.assertRaises(ValueError):p.guard(policy,s,at=NOW)
        pod['status']['initContainerStatuses'][0]['restartCount']=1
        pod['status']['containerStatuses'][0]['restartCount']=3
        with self.assertRaises(ValueError):p.guard(policy,s,at=NOW)
        pod['status']['containerStatuses'][0]['restartCount']=2
        pod['spec']['initContainers']=[{'resources':{'limits':{'cpu':'28'}}}]
        with self.assertRaises(ValueError):p.guard(policy,s,at=NOW)

    def test_resource_scope_and_security(self):
        policy,_=fixture();objects=p.objects(policy,{})
        self.assertEqual([x['kind'] for x in objects],['ConfigMap','PersistentVolumeClaim','Pod'])
        self.assertTrue(objects[0]['immutable']);spec=objects[2]['spec']
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertEqual(spec['containers'][0]['resources']['limits'],{'cpu':'1','memory':'1Gi'})
        self.assertEqual(spec['volumes'][0]['persistentVolumeClaim']['claimName'],policy['name'])
        self.assertNotIn('ownerReferences',objects[1]['metadata'])
        policy['name']='existing-business-volume'
        with self.assertRaises(ValueError):p.objects(policy,{})

    def test_bound_identity_multilabel_topology(self):
        policy,s=fixture();cm,pvc,pod=p.objects(policy,{})
        for value in (pvc,pod):value['metadata']=copy.deepcopy(value['metadata']);value['metadata']['uid']=U
        pod['spec']['nodeName']=policy['node'];pod['spec']['volumes'][-1]['configMap']['defaultMode']=420
        pod['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
        pvc['status']={'phase':'Bound','capacity':{'storage':'64Gi'}};pvc['spec']['volumeName']='new-pv'
        pv={'metadata':{'name':'new-pv','uid':U},'status':{'phase':'Bound'},'spec':{
            'claimRef':{'name':policy['name'],'namespace':'cicd','uid':U},'csi':{'driver':'directpv-min-io'},
            'accessModes':['ReadWriteOnce'],'nodeAffinity':{'required':{'nodeSelectorTerms':[{
                'matchExpressions':[{'key':k,'operator':'In','values':[v]}
                                    for k,v in s['nodes']['items'][0]['metadata']['labels'].items()]}]}}}}
        created=[{'kind':x,'uid':U} for x in ('Pod','PersistentVolumeClaim')]
        bound=p.bound_identity(policy,pod,pvc,pv,created,s['nodes']['items'][0]);self.assertEqual(bound['node_uid'],U)
        pv['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['values']=['other']
        with self.assertRaises(ValueError):p.bound_identity(policy,pod,pvc,pv,created,s['nodes']['items'][0])

    def test_no_automatic_retry_mutations(self):
        source=p.HERE.joinpath('node_cache_publish_control.py').read_text()
        self.assertNotIn("['apply'",source);self.assertNotIn("['patch'",source)
        self.assertIn("'preconditions':{'uid':uid}",source)
        self.assertIn("result['pending_create']",source)


if __name__=='__main__':unittest.main()
