import copy
import unittest
import json
import subprocess
import tempfile
from unittest import mock
import node_cache_fleet_control as f
from test_node_cache_publish_control import fixture,U,NOW


def original():
    return {'metadata':{'name':'cs-mty-large-7c14g','labels':{'app.kubernetes.io/version':'0.14.1'}},
       'spec':{'githubConfigUrl':'https://github.com/matrixorigin/matrixone',
               'githubConfigSecret':'cs-mty-large-7c14g-github-secret','listenerTemplate':{'spec':{'containers':[{'name':'listener','image':'fixture'}]}},
               'template':{'spec':{'containers':[{'name':'runner','image':'ghcr.io/actions/actions-runner:latest','resources':{
                   'requests':{'cpu':'7','memory':'12Gi'},'limits':{'cpu':'7','memory':'14Gi'}}}]}}}}


class Fleet(unittest.TestCase):
    def test_complete_lifecycle_creates_only_new_pool(self):
        policy,snapshot=fixture();binding={'environment':'mty','node':'mo-standard-0','node_uid':U,'pvc_uid':U,'pv_uid':U}
        receipt={'complete':True,'preflight':{'init_restarts':{}},
                 'verify':{'success':True,'binding':binding},'publish':{'binding':binding}}
        pvc={'metadata':{'name':policy['name'],'namespace':'cicd','uid':U},'status':{'phase':'Bound'},
             'spec':{'volumeName':'new-pv','storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce']}}
        pv={'metadata':{'name':'new-pv','uid':U},'status':{'phase':'Bound'},'spec':{
            'claimRef':{'name':policy['name'],'namespace':'cicd','uid':U},'csi':{'driver':'directpv-min-io'},
            'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce'],
            'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[
                {'key':k,'operator':'In','values':[v]} for k,v in snapshot['nodes']['items'][0]['metadata']['labels'].items()]}]}}}}
        old=original();old['metadata']['uid']=U;mutations=[]
        def execute(cmd,**kw):
            args=cmd[4:];result=None
            if args[0]=='create':
                obj=json.loads(kw['input']);obj['metadata']['uid']=U;mutations.append(obj);result=obj
            else:
                self.assertEqual(args[0],'get');kind=args[1]
                if kind=='namespace':result=snapshot['namespaces']
                elif kind=='nodes':result=snapshot['nodes']
                elif kind=='sc':result=snapshot['sc']
                elif kind=='directpvdrives':result=snapshot['drives']
                elif kind=='pods':result=snapshot['pods']
                elif kind=='--raw':result=snapshot['stats']
                elif kind=='pvc':result=pvc
                elif kind=='pv':result=pv
                elif kind=='autoscalingrunnersets':result={'items':[old]}
                elif kind=='autoscalingrunnerset' and args[2]==old['metadata']['name']:result=old
                else:self.assertIn(kind,('pod','autoscalingrunnerset'))
            return subprocess.CompletedProcess(cmd,0,'' if result is None else json.dumps(result),'')
        with tempfile.TemporaryDirectory() as tmp,mock.patch.object(f.subprocess,'run',side_effect=execute):
            result=f.run(policy,receipt,'fixture',f.Path(tmp).resolve()/'receipt.json')
        self.assertTrue(result['complete']);self.assertNotIn('pending_create',result)
        self.assertEqual(len(mutations),1);self.assertEqual(mutations[0]['kind'],'AutoscalingRunnerSet')
        self.assertEqual(mutations[0]['metadata']['name'],'cs-mty-nc-n0-v1')
        self.assertEqual(old['spec']['template']['spec']['containers'][0]['resources']['limits']['cpu'],'7')

    def test_multi_runner_same_readonly_pvc_no_original_mutation(self):
        policy,_=fixture();old=original();before=copy.deepcopy(old)
        binding={'environment':'mty','node':'mo-standard-0','node_uid':U,'pvc_uid':U,'pv_uid':U}
        obj=f.pool(policy,binding,old);spec=obj['spec'];pod=spec['template']['spec']
        self.assertEqual(old,before);self.assertEqual(spec['minRunners'],0);self.assertEqual(spec['maxRunners'],2)
        self.assertEqual(spec['runnerScaleSetLabels'],['cs-mty-node-cache-7c14g'])
        self.assertEqual(pod['volumes'][0],{'name':'work','emptyDir':{}})
        self.assertTrue(pod['volumes'][1]['persistentVolumeClaim']['readOnly'])
        self.assertTrue(pod['containers'][0]['volumeMounts'][1]['readOnly'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['containers'][0]['image'],f.pub.IMAGE)
        self.assertNotIn('TOKEN',str(pod['containers'][0]['env']))
        self.assertEqual(pod['nodeSelector']['kubernetes.io/hostname'],'mo-standard-0')

    def test_reject_template_image_environment_or_secret_drift(self):
        policy,_=fixture();binding={'environment':'mty','node':'mo-standard-0','node_uid':U,'pvc_uid':U,'pv_uid':U}
        for case in ('image','dind','version','environment','auth'):
            with self.subTest(case=case):
                old=original();b=dict(binding)
                if case=='image':old['spec']['template']['spec']['containers'][0]['image']='unknown'
                if case=='dind':old['spec']['template']['spec']['containers'].append({'name':'dind'})
                if case=='version':old['metadata']['labels']['app.kubernetes.io/version']='other'
                if case=='environment':b['environment']='jed'
                if case=='auth':old['spec']['githubConfigSecret']='business-secret'
                with self.assertRaises(ValueError):f.pool(policy,b,old)

    def test_two_reader_capacity_not_just_publisher_capacity(self):
        policy,snapshot=fixture();self.assertEqual(f.guard(policy,snapshot)['new_runner_limit'],2)
        snapshot['nodes']['items'][0]['status']['allocatable']['cpu']='15'
        with self.assertRaises(ValueError):f.guard(policy,snapshot)
        snapshot['nodes']['items'][0]['status']['allocatable']['cpu']='32'
        snapshot['stats']['node']['fs']['availableBytes']=59*f.c.GIB
        with self.assertRaises(ValueError):f.guard(policy,snapshot)

    def test_no_apply_update_delete_or_test_dispatch(self):
        source=f.Path(f.__file__).read_text()
        for word in ("['apply'","['patch'","['delete'",'workflow run','pulumi up'):
            self.assertNotIn(word,source)
        self.assertEqual(source.count("call(['create'"),1)
        self.assertIn("result['pending_create']",source)


if __name__=='__main__':unittest.main()
