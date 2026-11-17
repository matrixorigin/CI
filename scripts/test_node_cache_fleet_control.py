import copy
import unittest
import node_cache_fleet_control as f
from test_node_cache_publish_control import fixture,U,NOW


def original():
    return {'metadata':{'name':'cs-mty-large-7c14g','labels':{'app.kubernetes.io/version':'0.14.1'}},
       'spec':{'githubConfigUrl':'https://github.com/matrixorigin/matrixone',
               'githubConfigSecret':'cs-mty-large-7c14g-github-secret','listenerTemplate':{'spec':{'containers':[{'name':'listener','image':'fixture'}]}},
               'template':{'spec':{'containers':[{'name':'runner','image':f.pub.IMAGE,'resources':{
                   'requests':{'cpu':'7','memory':'12Gi'},'limits':{'cpu':'7','memory':'14Gi'}}}]}}}}


class Fleet(unittest.TestCase):
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
