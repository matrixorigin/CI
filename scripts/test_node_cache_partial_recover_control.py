import copy
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import node_cache_partial_recover_control as q
import node_cache_replicate_control as b
import node_cache_partial_replication as p
import node_cache_chunk_replication as chunks
import test_node_cache_replication as fixtures
from test_node_cache_publish_control import fixture,NOW


class PartialController(unittest.TestCase):
    def exercise(self,bad=None):
        data=fixtures.Replication('test_two_nodes_same_objects_new_bindings_source_unchanged')
        data.environment='jed';data.setUp();self.addCleanup(data.doCleanups)
        policy,s=fixture();policy.update(environment='jed',name='cs-jed-incremental-n0-v1')
        policy['namespace_uids']={k.replace('mocservices-mty','mocservices-jed'):v for k,v in policy['namespace_uids'].items()}
        s['namespaces']['items']=[{'metadata':{'name':k,'uid':v}} for k,v in policy['namespace_uids'].items()]
        policy['node_uids']={f'mo-standard-{i}':data.source['node_uid'] if i==0 else data.target(i)['node_uid'] for i in range(3)}
        s['nodes']['items']=[{**copy.deepcopy(s['nodes']['items'][0]),'metadata':{'name':name,'uid':uid,
            'labels':{'directpv.min.io/node':name,'directpv.min.io/tier':'ephemeral'}}} for name,uid in policy['node_uids'].items()]
        s['drives']['items']=[{'metadata':{'labels':n['metadata']['labels']},'status':{'status':'Ready','freeCapacity':200*b.c.GIB}} for n in s['nodes']['items']]
        live={};mutations=[];counter=10;roots={policy['name']:data.source_root};target=data.target(env='jed')
        def pvc(claim,node,identity):
            claim_obj={'metadata':{'name':claim,'namespace':'cicd','uid':identity['pvc_uid']},'status':{'phase':'Bound','capacity':{'storage':'64Gi'}},
                'spec':{'volumeName':claim+'-pv','accessModes':['ReadWriteOnce'],'storageClassName':'directpv-min-io-ephemeral'}}
            pv_obj={'metadata':{'name':claim+'-pv','uid':identity['pv_uid']},'status':{'phase':'Bound'},'spec':{
                'claimRef':{'name':claim,'namespace':'cicd','uid':identity['pvc_uid']},'accessModes':['ReadWriteOnce'],
                'storageClassName':'directpv-min-io-ephemeral','csi':{'driver':'directpv-min-io'},
                'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'directpv.min.io/node','operator':'In','values':[node]}]}]}}}}
            live['pvc',claim]=claim_obj;live['pv',claim+'-pv']=pv_obj
        pvc(policy['name'],'mo-standard-0',data.source);pvc('cs-jed-incremental-n1-v1','mo-standard-1',target)
        destination=data.root/'partial';destination.mkdir();roots['cs-jed-incremental-n1-v1']=destination
        truncated=data.rewrite(lambda rows:[x for x in rows if x[0].name!=p.tool_name(data.lock)])
        with self.assertRaises(ValueError):b.replication.receive(destination,data.catalog,data.source,data.lock,target,io.BytesIO(truncated))
        existing=destination/'.replication-stage'/data.object_name;before=(existing.stat().st_ino,existing.stat().st_mtime_ns,existing.read_bytes())
        oldcm='cs-jed-cache-replication-v3'
        made=[{'kind':kind,'name':name,'uid':f'80000000-0000-4000-8000-{i:012d}'} for i,(kind,name) in enumerate([
            ('ConfigMap',oldcm),('Pod',oldcm+'-source'),('PersistentVolumeClaim','cs-jed-incremental-n1-v1'),('Pod',oldcm+'-n1')],1)]
        made[2]['uid']=target['pvc_uid'];live['configmap',oldcm]={'metadata':{'uid':made[0]['uid']},'immutable':True}
        for row,node,claim,ro in [(made[1],'mo-standard-0',policy['name'],True),(made[3],'mo-standard-1','cs-jed-incremental-n1-v1',False)]:
            pod=b.helper(row['name'],node,claim,oldcm,ro);pod['metadata']['uid']=row['uid'];pod['spec']['nodeName']=node
            pod['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]};live['pod',row['name']]=pod
        plan=b.replication.make_plan(data.source_root,data.catalog,data.source,data.lock)
        payload=b.replication.expected_files(plan,data.catalog,data.source,data.lock)
        approval={'original_run_id':q.ORIGINAL,'receipt':{'environment':'jed','complete':False,'error_type':'ValueError',
            'failure_sites':[{'file':'node_cache_replicate_control.py','line':242},{'file':'node_cache_replicate_control.py','line':116}],
            'deleted':[],'created':made,'started_at':'original-start','targets':{'mo-standard-1':{'binding':target}},
            'payload':{'bytes':sum(x[0] for x in payload.values()),'files':len(payload)}},
            'stage_reverification':{'original_run_id':q.ORIGINAL,'environment':'jed','readonly':True,'mutations':0,'bad':[],
                'verified_files':len(payload)-1,'expected_files':len(payload),'verified_bytes':sum(x[0] for x in payload.values())-data.lock['archive_size'],
                'missing':[p.tool_name(data.lock)],'published':False,'compiler_invocations':0,'external_downloads':0}}
        if bad=='uid':live['pod',made[1]['name']]['metadata']['uid']='replacement'
        if bad=='proof':approval['stage_reverification']['bad']=['wrong']
        if bad=='hash':existing.chmod(0o644);existing.write_bytes(b'X'*existing.stat().st_size)
        def api(path):return {'head_sha':q.OLD_REV,'head_branch':'codex/ruh-pvc-readiness','event':'workflow_dispatch',
            'conclusion':'failure','status':'completed','run_attempt':1,'path':'.github/workflows/cloudsigma-up.yaml','actor':{'login':'Ariznawlll'}}
        def call(command,**kwargs):
            nonlocal counter
            args=command[4:];verb=args[0];value=None
            if verb=='get':
                kind=args[1].lower()
                if kind=='namespace':value=s['namespaces']
                elif kind=='nodes':value=s['nodes']
                elif kind=='pods':value={'items':[v for (k,_),v in live.items() if k=='pod']}
                elif kind=='sc':value=s['sc']
                elif kind=='directpvdrives':value=s['drives']
                elif kind=='--raw':value=s['stats']
                else:value=live.get(({'persistentvolumeclaim':'pvc'}.get(kind,kind),args[2]))
            elif verb=='create':
                obj=json.loads(kwargs['input']);counter+=1;name=obj['metadata']['name'];kind=obj['kind']
                obj['metadata']['uid']=f'90000000-0000-4000-8000-{counter:012d}';key={'Pod':'pod','ConfigMap':'configmap','PersistentVolumeClaim':'pvc'}[kind]
                live[key,name]=obj;mutations.append(('create',kind,name))
                if kind=='Pod':
                    node=obj['spec']['nodeSelector']['kubernetes.io/hostname'];obj['spec']['nodeName']=node
                    obj['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
                    claim=obj['spec']['volumes'][0]['persistentVolumeClaim']['claimName']
                    if claim not in roots:
                        identity={**data.target(2,env='jed'),'pvc_uid':live['pvc',claim]['metadata']['uid']}
                        pvc(claim,node,identity);roots[claim]=data.root/'node2';roots[claim].mkdir()
                value=obj
            elif verb=='delete':
                name=args[2].split('/')[-1];options=json.loads(kwargs['input'])
                self.assertEqual(options['preconditions']['uid'],live['pod',name]['metadata']['uid'])
                del live['pod',name];mutations.append(('delete','Pod',name));value={'status':'Success'}
            elif verb=='exec':
                name=args[1];script=next(x for x in args if x.startswith('/probe/'));mode=args[args.index(script)+1]
                claim=live['pod',name]['spec']['volumes'][0]['persistentVolumeClaim']['claimName'];root=roots[claim]
                identity=json.loads(args[args.index('--target-json')+1]) if '--target-json' in args else None
                if mode=='inspect':
                    self.assertEqual(kwargs['timeout'],600);_,expected=p.inspect(root,data.catalog,data.source,data.lock,identity)
                    value={'success':True,'verified_files':len(expected)-1,'missing':[p.tool_name(data.lock)]}
                elif mode=='verify':
                    self.assertEqual(kwargs['timeout'],600);value=b.replication.verify(root,plan,data.catalog,data.source,data.lock,identity)
                elif mode=='plan-batches':
                    expected,groups=chunks.batches(plan,data.catalog,data.source,data.lock)
                    value={'batches':len(groups),'bytes':sum(x[0] for x in expected.values()),'files':len(expected)}
                elif mode=='finish':value=chunks.finish(root,data.catalog,data.source,data.lock,identity)
                else:raise AssertionError(mode)
            else:raise AssertionError(verb)
            return subprocess.CompletedProcess(command,0,'' if value is None else json.dumps(value),'')
        def streaming(source_cmd,target_cmd,**kwargs):
            args=target_cmd[4:];name=args[1];script=next(x for x in args if x.startswith('/probe/'));mode=args[args.index(script)+1]
            identity=json.loads(args[args.index('--target-json')+1]);claim=live['pod',name]['spec']['volumes'][0]['persistentVolumeClaim']['claimName']
            if mode=='finish-tool':
                out=io.BytesIO();p.export_tool(data.source_root,data.catalog,data.source,data.lock,out);self.assertEqual(len(out.getvalue()),data.lock['archive_size'])
                out.seek(0);return p.finish_tool(roots[claim],data.catalog,data.source,data.lock,identity,out)
            self.assertEqual(mode,'receive-batch');self.assertLessEqual(kwargs['timeout'],900)
            number=int(args[args.index('--number')+1]);out=io.BytesIO()
            chunks.export_batch(data.source_root,data.catalog,data.source,data.lock,number,out);out.seek(0)
            return chunks.receive_batch(roots[claim],data.catalog,data.source,data.lock,identity,number,out)
        with patch.object(q.subprocess,'run',side_effect=call),patch.object(b.pub,'now',return_value=NOW), \
             patch.object(b.n,'reviewed_entries',side_effect=lambda cat,*_:cat['entries']),patch.object(b.tools,'authorize'), \
             patch.object(b,'stream_transfer',side_effect=streaming) as transfer:
            if bad:
                with self.assertRaises(ValueError):q.run(policy,data.catalog,data.lock,approval,'private',data.root/'recovery',api=api)
                self.assertEqual(transfer.call_count,0)
                if bad in ('uid','proof'):self.assertEqual(mutations,[])
                return
            result=q.run(policy,data.catalog,data.lock,approval,'private',data.root/'recovery',api=api)
            self.assertEqual(transfer.call_count,2)
        obj=destination/data.object_name
        self.assertEqual(before,(obj.stat().st_ino,obj.stat().st_mtime_ns,obj.read_bytes()))
        self.assertTrue(result['complete']);self.assertTrue(result['helper_pods_absent'])
        self.assertEqual(result['retired_helpers'],[made[1],made[3]])
        self.assertEqual(len(result['deleted']),3)
        self.assertEqual([k for v,k,n in mutations if v=='create'],['ConfigMap','Pod','Pod','PersistentVolumeClaim','Pod'])
        self.assertIn(('pvc','cs-jed-incremental-n1-v1'),live);self.assertIn(('configmap',oldcm),live)
        self.assertEqual(len([k for k,n in live if k=='pvc']),3)

    def test_exact_scope_partial_tool_and_only_missing_node2(self):self.exercise()
    def test_replaced_old_helper_refuses_all_mutations(self):self.exercise('uid')
    def test_bad_approved_proof_refuses_all_mutations(self):self.exercise('proof')
    def test_live_corrupt_stage_never_streamed_or_published(self):self.exercise('hash')
