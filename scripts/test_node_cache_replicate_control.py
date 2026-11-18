import copy
import datetime as dt
import unittest
from unittest.mock import patch
import json
import subprocess
import io
import sys

import node_cache_replicate_control as r
from test_node_cache_publish_control import fixture, NOW, U


class ControllerGuard(unittest.TestCase):
    def test_full_copy_controller_only_creates_new_resources_uid_deletes_own_pods(self):
        self.exercise_controller()

    def test_resume_reverifies_node1_without_recopy_creates_only_missing_node2(self):
        self.exercise_controller(recover=True)

    def test_resume_rejects_replaced_helper_before_any_new_copy(self):
        self.exercise_controller(recover=True,bad_uid=True)

    def exercise_controller(self,recover=False,bad_uid=False):
        from test_node_cache_replication import Replication
        data=Replication('test_two_nodes_same_objects_new_bindings_source_unchanged')
        data.environment='mnl2' if recover else 'mty';data.setUp();self.addCleanup(data.doCleanups)
        policy,s=fixture();env=data.environment;policy.update(environment=env,name=f'cs-{env}-incremental-n0-v1')
        policy['namespace_uids']={k.replace('mocservices-mty','mocservices-'+env):v for k,v in policy['namespace_uids'].items()}
        s['namespaces']['items']=[{'metadata':{'name':k,'uid':v}} for k,v in policy['namespace_uids'].items()]
        policy['node_uids']={f'mo-standard-{i}':data.source['node_uid'] if i==0 else data.target(i)['node_uid'] for i in range(3)}
        s['nodes']['items']=[{**copy.deepcopy(s['nodes']['items'][0]),'metadata':{'name':name,'uid':uid,
              'labels':{'directpv.min.io/node':name,'directpv.min.io/tier':'ephemeral'}}} for name,uid in policy['node_uids'].items()]
        s['drives']['items']=[{'metadata':{'labels':x['metadata']['labels']},'status':{'status':'Ready','freeCapacity':200*r.c.GIB}}
                              for x in s['nodes']['items']]
        live={};mutations=[];destinations={};counter=10;fail_first_verify=recover
        source_pvc={'metadata':{'name':policy['name'],'uid':data.source['pvc_uid']},'status':{'phase':'Bound'},'spec':{'volumeName':'source-pv'}}
        def pv(pvc,node,uid):
            return {'metadata':{'name':pvc['spec']['volumeName'],'uid':uid},'status':{'phase':'Bound'},'spec':{
                'claimRef':{'name':pvc['metadata']['name'],'namespace':'cicd','uid':pvc['metadata']['uid']},
                'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce'],
                'csi':{'driver':'directpv-min-io'},'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[
                    {'key':'directpv.min.io/node','operator':'In','values':[node]}]}]}}}}
        live['pvc',policy['name']]=source_pvc;live['pv','source-pv']=pv(source_pvc,'mo-standard-0',data.source['pv_uid'])
        def api(path):
            return {'head_sha':'ecc89fdfec0b7acd575fb9ef563f6f1d06b9fad6','head_branch':'codex/ruh-pvc-readiness',
                    'event':'workflow_dispatch','status':'completed','conclusion':'failure','run_attempt':1,
                    'path':'.github/workflows/cloudsigma-up.yaml','actor':{'login':'Ariznawlll'}} if path.endswith('/44269605748') else {}
        def call(command,**kwargs):
            nonlocal counter,fail_first_verify
            args=command[4:];verb=args[0];result=None
            if verb=='create':
                obj=json.loads(kwargs['input']);counter+=1;name=obj['metadata']['name'];kind=obj['kind']
                obj['metadata']['uid']=f'90000000-0000-4000-8000-{counter:012d}'
                live[{'Pod':'pod','PersistentVolumeClaim':'pvc','ConfigMap':'ConfigMap'}[kind],name]=obj
                mutations.append(('create',kind,name))
                if kind=='Pod':
                    obj['metadata']['creationTimestamp']=NOW.isoformat()
                    node=obj['spec']['nodeSelector']['kubernetes.io/hostname'];obj['spec']['nodeName']=node
                    obj['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
                    claim=obj['spec']['volumes'][0]['persistentVolumeClaim']['claimName']
                    if claim!=policy['name']:
                        pvc=live['pvc',claim];pvc['status']={'phase':'Bound','capacity':{'storage':'64Gi'}}
                        pvc['spec']['volumeName']=claim+'-pv';live['pv',claim+'-pv']=pv(pvc,node,f'a0000000-0000-4000-8000-{counter:012d}')
                        destinations[name]=data.root/name;destinations[name].mkdir()
                result=obj
            elif verb=='get':
                kind=args[1]
                if kind=='namespace':result=s['namespaces']
                elif kind=='nodes':result=s['nodes']
                elif kind=='pods':result={'items':[v for (k,name),v in live.items() if k=='pod']}
                elif kind=='sc':result=s['sc']
                elif kind=='directpvdrives':result=s['drives']
                elif kind=='--raw':result=s['stats']
                else:result=live.get(('ConfigMap' if kind=='configmap' else kind,args[2]))
            elif verb=='exec':
                pod=args[1];mode=args[args.index('/probe/node_cache_replication.py')+1]
                if mode=='plan':result=r.replication.make_plan(data.source_root,data.catalog,data.source,data.lock)
                elif mode=='export':kwargs['stdout'].write(data.bundle());return subprocess.CompletedProcess(command,0,None,b'')
                elif mode=='receive':
                    target=json.loads(args[args.index('--target-json')+1])
                    result=r.replication.receive(destinations[pod],data.catalog,data.source,data.lock,target,kwargs['stdin'])
                elif mode=='verify':
                    self.assertEqual(kwargs['timeout'],600)
                    if fail_first_verify:
                        fail_first_verify=False;raise subprocess.TimeoutExpired(command,45)
                    target=json.loads(args[args.index('--target-json')+1]);plan=r.replication.make_plan(data.source_root,data.catalog,data.source,data.lock)
                    result=r.replication.verify(destinations[pod],plan,data.catalog,data.source,data.lock,target)
                else:raise AssertionError(mode)
            elif verb=='delete':
                name=args[2].split('/')[-1];options=json.loads(kwargs['input'])
                self.assertEqual(options['preconditions']['uid'],live['pod',name]['metadata']['uid'])
                del live['pod',name];mutations.append(('delete','Pod',name));result={'status':'Success'}
            else:raise AssertionError(args)
            return subprocess.CompletedProcess(command,0,'' if result is None else json.dumps(result),'')
        with patch.object(r.subprocess,'run',side_effect=call),patch.object(r.pub,'now',return_value=NOW), \
             patch.object(r.n,'reviewed_entries',side_effect=lambda catalog,runtime,api:catalog['entries']),patch.object(r.tools,'authorize'), \
             patch.object(r,'stream_transfer',side_effect=lambda source,target:json.loads(call(target,stdin=io.BytesIO(data.bundle())).stdout)) as streaming:
            if not recover:result=r.run(policy,data.catalog,data.lock,'config',data.root/'controller',api=api)
            else:
                with self.assertRaises(subprocess.TimeoutExpired):
                    r.run(policy,data.catalog,data.lock,'config',data.root/'controller',api=api)
                old=json.loads((data.root/'controller/state.json').read_text())
                self.assertEqual(set(old['targets']),{'mo-standard-1'})
                self.assertTrue(old['targets']['mo-standard-1']['received']['success'])
                # The approved report came from the original pinned version;
                # retain its exact diagnostic sites, not the new line numbers.
                old['failure_sites']=[{'file':'node_cache_replicate_control.py','line':244},
                                      {'file':'node_cache_replicate_control.py','line':141}]
                resume={'original_run_id':44269605748,'receipt':old,'node1_reverification':{
                    'original_run':44269605748,'readonly':True,'mutations':0,
                    'proof':copy.deepcopy(old['targets']['mo-standard-1']['received'])}}
                if bad_uid:
                    live['pod','cs-mnl2-cache-replication-v3-source']['metadata']['uid']='replacement'
                    before=copy.deepcopy(mutations)
                    with self.assertRaises(ValueError):r.run(policy,data.catalog,data.lock,'config',data.root/'resume',api=api,resume=resume)
                    self.assertEqual(mutations,before);self.assertEqual(streaming.call_count,1)
                    return
                result=r.run(policy,data.catalog,data.lock,'config',data.root/'resume',api=api,resume=resume)
                self.assertTrue(result['node1_not_recopied']);self.assertEqual(len(result['adopted_resources']),4)
            self.assertEqual(streaming.call_count,2)
        self.assertTrue(result['complete']);self.assertTrue(result['helper_pods_absent'])
        self.assertEqual(len(result['targets']),2);self.assertEqual(len(result['deleted']),3)
        self.assertEqual([kind for verb,kind,name in mutations if verb=='create'],
                         ['ConfigMap','Pod','PersistentVolumeClaim','Pod','PersistentVolumeClaim','Pod'])
        self.assertTrue(all(kind=='Pod' for verb,kind,name in mutations if verb=='delete'))
        self.assertIn(('pvc',policy['name']),live)
        self.assertEqual(result['compiler_invocations'],0)
        self.assertEqual(result['payload']['controller_archive_bytes'],0)

    def test_real_bounded_pipe_uses_no_archive_file(self):
        source=[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*1048576)']
        target=[sys.executable,'-c','import sys,json,hashlib;data=sys.stdin.buffer.read();print(json.dumps({"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}))']
        result=r.stream_transfer(source,target,timeout=10)
        self.assertEqual(result['bytes'],1048576)

    def test_producer_waits_for_consumer_ack_without_eof_deadlock(self):
        source=[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*1048576);sys.stdout.buffer.flush();assert sys.stdin.buffer.read(1)==b"A"']
        target=[sys.executable,'-c','import sys,json;data=sys.stdin.buffer.read(1048576);assert len(data)==1048576;print(json.dumps({"verified":True,"bytes":len(data)}))']
        self.assertEqual(r.stream_transfer(source,target,timeout=10,await_ack=True)['bytes'],1048576)

    def test_rejected_consumer_is_not_acknowledged(self):
        source=[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*1024);sys.stdout.buffer.flush();assert sys.stdin.buffer.read(1)==b"A"']
        target=[sys.executable,'-c','import sys;sys.stdin.buffer.read(1024);print("{\"success\":false}")']
        with self.assertRaises(ValueError):r.stream_transfer(source,target,timeout=10,await_ack=True)

    def test_pipe_failure_is_not_accepted(self):
        for source,target in [([sys.executable,'-c','raise SystemExit(2)'],
                               [sys.executable,'-c','import sys;sys.stdin.buffer.read();print("{}")']),
                              ([sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*1048576)'],
                               [sys.executable,'-c','raise SystemExit(3)'])]:
            with self.assertRaises(ValueError):r.stream_transfer(source,target,timeout=10)

    def test_transfer_failure_keeps_only_allowlisted_diagnostics(self):
        source=[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*1048576)']
        target=[sys.executable,'-c','import sys,json;print(json.dumps({"error_type":"ReadError","private":"PRIVATE_DIAGNOSTIC_SENTINEL"}),file=sys.stderr);print("i/o timeout PRIVATE_DIAGNOSTIC_SENTINEL",file=sys.stderr);raise SystemExit(3)']
        with self.assertRaises(ValueError) as failed:r.stream_transfer(source,target,timeout=10)
        value=failed.exception.transfer_diagnostic
        self.assertEqual(value['target_exit_code'],3);self.assertEqual(value['receiver_error_types'],['ReadError'])
        self.assertEqual(value['transport_signal_codes'],['io_timeout'])
        self.assertFalse(value['stderr_text_retained']);self.assertNotIn('PRIVATE_DIAGNOSTIC_SENTINEL',json.dumps(value))

    def test_small_copy_pod_guard_is_not_a_ut_runner_gate(self):
        policy,snapshot=fixture()
        snapshot['stats']['node']['memory']['availableBytes']=17*r.c.GIB
        snapshot['nodes']['items'][0]['status']['allocatable']['memory']='18Gi'
        with patch.object(r.pub,'now',return_value=NOW):
            result=r.snapshot_guard(policy,snapshot,'mo-standard-0',allocate=True)
        self.assertEqual(result['workloads'],{})

    def test_namespace_pressure_expired_disk_cpu_and_running_test_block(self):
        for reason in ('namespace','node','pressure','stale','root','memory','cpu','drive','runner','limits'):
            with self.subTest(reason=reason):
                policy,s=fixture()
                if reason=='namespace':s['namespaces']['items'][0]['metadata']['uid']='changed'
                if reason=='node':s['nodes']['items'][0]['metadata']['uid']='changed'
                if reason=='pressure':s['nodes']['items'][0]['status']['conditions'][1]['status']='True'
                if reason=='stale':s['stats']['node']['cpu']['time']=(NOW-dt.timedelta(seconds=121)).isoformat()
                if reason=='root':s['stats']['node']['fs']['availableBytes']=59*r.c.GIB
                if reason=='memory':s['stats']['node']['memory']['availableBytes']=3*r.c.GIB
                if reason=='cpu':s['stats']['node']['cpu']['usageNanoCores']=6e9
                if reason=='drive':s['drives']['items'][0]['status']['freeCapacity']=127*r.c.GIB
                if reason=='runner':s['pods']['items']=[{'metadata':{'ownerReferences':[{'kind':'EphemeralRunner'}]},'status':{'phase':'Pending'}}]
                if reason=='limits':s['nodes']['items'][0]['status']['allocatable']['cpu']='2'
                with patch.object(r.pub,'now',return_value=NOW),self.assertRaises(ValueError):
                    r.snapshot_guard(policy,s,'mo-standard-0',allocate=True)

    def test_workload_restart_or_replacement_refused(self):
        policy,s=fixture()
        pod={'metadata':{'name':'cn','namespace':'business','uid':U,'ownerReferences':[{'kind':'CloneSet'}]},
             'spec':{'nodeName':'mo-standard-0','containers':[{'resources':{'limits':{'cpu':'8','memory':'32Gi'}}}]},
             'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}],
                       'containerStatuses':[{'restartCount':0}]}}
        s['pods']['items']=[pod]
        with patch.object(r.pub,'now',return_value=NOW):
            base=r.snapshot_guard(policy,s,'mo-standard-0')['workloads']
            for field in ('restart','uid'):
                bad=copy.deepcopy(s)
                if field=='restart':bad['pods']['items'][0]['status']['containerStatuses'][0]['restartCount']=1
                else:bad['pods']['items'][0]['metadata']['uid']='replacement'
                with self.assertRaises(ValueError):r.snapshot_guard(policy,bad,'mo-standard-0',baseline=base)

    def test_copy_pods_are_bounded_no_token_source_double_readonly(self):
        for readonly in (False,True):
            spec=r.helper('new-helper','mo-standard-0','reviewed-claim','new-config',readonly)['spec']
            self.assertFalse(spec['automountServiceAccountToken'])
            self.assertEqual(spec['activeDeadlineSeconds'],4500)
            self.assertEqual(spec['containers'][0]['resources']['limits'],{'cpu':'1','memory':'512Mi'})
            self.assertEqual(spec['containers'][0]['volumeMounts'][0].get('readOnly',False),readonly)
            self.assertEqual(spec['volumes'][0]['persistentVolumeClaim'].get('readOnly',False),readonly)
            self.assertNotIn('serviceAccountName',spec)
            changed=copy.deepcopy(spec);changed['containers'][0]['image']='other'
            self.assertFalse(r.subset(spec,changed))

    def test_exact_admission_defaults_allowed_without_weakening_template(self):
        expected=r.helper('helper','mo-standard-0','cache','config',True)['spec']
        actual=copy.deepcopy(expected)
        actual['tolerations'] += [{'effect':'NoExecute','key':key,'operator':'Exists','tolerationSeconds':300}
            for key in ('node.kubernetes.io/not-ready','node.kubernetes.io/unreachable')]
        self.assertTrue(r.checked_helper_spec(expected,actual))
        self.assertEqual(len(actual['tolerations']),3)
        bad=copy.deepcopy(actual);bad['tolerations'][-1]['tolerationSeconds']=999
        self.assertFalse(r.checked_helper_spec(expected,bad))
        bad=copy.deepcopy(actual);bad['containers'][0]['volumeMounts'][0]['readOnly']=False
        self.assertFalse(r.checked_helper_spec(expected,bad))
        bad=copy.deepcopy(actual);bad['tolerations'].append({'operator':'Exists'})
        self.assertFalse(r.checked_helper_spec(expected,bad))


if __name__=='__main__':unittest.main()
