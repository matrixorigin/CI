"""RUH approval and fail-closed diagnostics; JED full path covered separately."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import node_cache_chunk_resume_control as q


def approval():
    rows=[{'kind':kind,'name':name,'uid':f'uid-{i}'} for i,(kind,name) in enumerate([
        ('ConfigMap','cs-ruh-cache-partial-r1'),('Pod','cs-ruh-cache-partial-r1-source'),
        ('Pod','cs-ruh-cache-partial-r1-n1'),('PersistentVolumeClaim','cs-ruh-incremental-n2-v1'),
        ('Pod','cs-ruh-cache-partial-r1-n2')])]
    batches=[{'number':i,'batches':36,'bytes':1,'files':1,'success':True,
        'compiler_invocations':0,'external_downloads':0} for i in range(18)]
    one={'success':True,'files':36,'bytes':36}
    old={'environment':'ruh','complete':False,'error_type':'ValueError','deleted':[],
        'failure_sites':[{'file':'node_cache_partial_recover_control.py','line':257},
            {'file':'node_cache_replicate_control.py','line':133}], 'created':rows,
        'targets':{'mo-standard-1':{'received':one,'verify':one},'mo-standard-2':{'batches':batches}}}
    proof={'environment':'ruh','inspected_run_id':q.RUH_PRIOR,'complete':True,'readonly':True,
        'filesystem_writes':0,'created':[],'deleted':[],'compiler_invocations':0,'external_downloads':0,
        'helpers':{r['name']:{'uid':r['uid'],'restarts':0,'memory_events':{'oom_kill':0}} for r in rows if r['kind']=='Pod'},
        'targets':{'mo-standard-1':one,'mo-standard-2':{'success':True,'readonly':True,'filesystem_writes':0,
            'batches':36,'acknowledged':list(range(18)),'expected_files':36,'expected_bytes':36,'verified_bytes':18,
            'acknowledged_receipts':batches}}}
    return {'prior_run_id':q.RUH_PRIOR,'receipt':old,'inspection_run_id':44290243774,'inspection':proof,
        'ack_control':{'readonly':True,'filesystem_writes':0,'producer_waited_for_ack':True,
            'batch':2,'verified_files':556,'verified_bytes':264928544}}


class ChunkScope(unittest.TestCase):
    def test_exact_ruh_scope_and_budget(self):
        a=approval();self.assertEqual(q.approval_scope(a,{'environment':'ruh'}),a['receipt'])
        self.assertEqual(q.scope('ruh'),(44282517875,q.RUH_REV,'cs-ruh-cache-chunks-r2','cs-ruh-cache-partial-r1',7200,9000,18))
        self.assertEqual(q.scope('jed'),(q.PRIOR,q.PRIOR_REV,q.NAME,'cs-jed-cache-partial-r2',3700,4500,2))

    def test_wrong_failure_scope_or_old_helper_refused(self):
        for mutate in (lambda a:a.update(prior_run_id=1),
            lambda a:a['receipt']['failure_sites'][0].update(line=233),
            lambda a:a['receipt']['created'][4].update(name='another-pod'),
            lambda a:a['receipt']['targets']['mo-standard-2']['batches'].pop()):
            a=copy.deepcopy(approval());mutate(a)
            with self.assertRaises(ValueError):q.approval_scope(a,{'environment':'ruh'})

    def test_replaced_restarted_or_oom_helper_refused(self):
        name='cs-ruh-cache-partial-r1-source'
        for change in ({'uid':'replacement'},{'restarts':1},{'memory_events':{'oom_kill':1}}):
            a=approval();a['inspection']['helpers'][name].update(change)
            with self.assertRaises(ValueError):q.approval_scope(a,{'environment':'ruh'})

    def test_inventory_and_source_proof_cannot_be_omitted(self):
        for mutate in (lambda a:a['inspection'].update(filesystem_writes=1),
            lambda a:a.update(inspection_run_id=1),
            lambda a:a['inspection']['targets']['mo-standard-2'].update(verified_bytes=17),
            lambda a:a['ack_control'].update(producer_waited_for_ack=False)):
            a=approval();mutate(a)
            with self.assertRaises(ValueError):q.approval_scope(a,{'environment':'ruh'})


class RuhController(unittest.TestCase):
    def exercise(self,changed_inventory=False):
        a=approval();old=a['receipt'];one=old['targets']['mo-standard-1']['verify']
        one.update(files=42,bytes=42)
        policy={'environment':'ruh','name':'cs-ruh-incremental-n0-v1',
            'node_uids':{f'mo-standard-{i}':f'node-{i}' for i in range(3)}}
        def binding(i):return {'environment':'ruh','node':f'mo-standard-{i}',
            'node_uid':f'node-{i}','pvc_uid':f'pvc-{i}','pv_uid':f'pv-{i}'}
        for i in (1,2):old['targets'][f'mo-standard-{i}']['binding']=binding(i)
        inv=a['inspection']['targets']['mo-standard-2']
        inv.update(expected_files=42,expected_bytes=42,verified_files=19,verified_bytes=19,
            pending=list(range(18,36)),partial_selections={'18':[f'missing-{i}' for i in range(6)]},
            compiler_invocations=0,external_downloads=0)
        final2={'success':True,'files':42,'bytes':42,'binding':binding(2)}
        source=binding(0);catalog={'nodes':[source]};lock={'producer':{'repository':'approved/repo','id':7},'artifact':{'id':8}}
        calls=[];made=[];transfers=[];checks=[]
        class Fake(q.Session):
            def __init__(self,*args):
                super().__init__(*args)
                self.live={('configmap',old['created'][0]['name']):{'metadata':{'uid':old['created'][0]['uid']},'immutable':True}}
                for index,row in enumerate(r for r in old['created'] if r['kind']=='Pod'):
                    pod=self.helper(row['name'],f'mo-standard-{index}',policy['name'] if index==0 else f'cs-ruh-incremental-n{index}-v1',old['created'][0]['name'],index==0)
                    pod['metadata']['uid']=row['uid'];self.live['pod',row['name']]=pod
            def guard(self,node):checks.append(node);return {'workloads':[]}
            def bound(self,node,claim,expected):
                assert expected==binding(int(node[-1]));return expected
            def call(self,args,data=None,timeout=45):
                calls.append(args)
                if args[0]=='get':return self.live.get((args[1].lower(),args[2]))
                if args[0]=='create':
                    value=copy.deepcopy(data);assert value['kind'] in ('ConfigMap','Pod')
                    value['metadata']['uid']=f'new-{len(made)}';made.append(value)
                    if value['kind']=='Pod':
                        value['spec']['nodeName']=value['spec']['nodeSelector']['kubernetes.io/hostname']
                        value['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
                    self.live[value['kind'].lower(),value['metadata']['name']]=value;return value
                if args[0]=='delete':
                    name=args[2].split('/')[-1];assert data['preconditions']['uid']==self.live['pod',name]['metadata']['uid']
                    self.live.pop(('pod',name));return {'status':'Success'}
                assert args[0]=='exec'
                script=next(x for x in args if x.startswith('/probe/'));mode=args[args.index(script)+1]
                assert timeout==600
                if mode=='inventory':
                    result=copy.deepcopy(inv)
                    if changed_inventory:result['verified_bytes']+=1
                    return result
                return copy.deepcopy(one if args[1].endswith('-n1') else final2)
        def api(path):
            inspection=path.endswith('/44290243774')
            return {'head_sha':'41ef9445611c8b7b8eaa9510f64ac65dbe67207e' if inspection else q.RUH_REV,
                'head_branch':'codex/ruh-pvc-readiness','event':'workflow_dispatch','status':'completed',
                'conclusion':'success' if inspection else 'failure','run_attempt':1,
                'path':'.github/workflows/cloudsigma-up.yaml','actor':{'login':'Ariznawlll'}}
        def stream(src,dst,**kwargs):
            assert kwargs['await_ack'] is True and 0<kwargs['timeout']<=900
            assert '-i' in src[:src.index('--')] and '-i' in dst[:dst.index('--')] and '--await-ack' in src
            assert dst[dst.index('exec')+1].endswith('-n2')
            number=int(dst[dst.index('--number')+1]);transfers.append(number)
            if number==18:
                assert json.loads(src[src.index('--selected-json')+1])==inv['partial_selections']['18']
            else:assert '--selected-json' not in src
            return {'success':True,'number':number,'batches':36,'files':7 if number==18 else 1,
                'bytes':7 if number==18 else 1,'transferred_bytes':6 if number==18 else 1,
                'compiler_invocations':0,'external_downloads':0}
        with tempfile.TemporaryDirectory() as raw,patch.object(q,'Session',Fake), \
             patch.object(q.b.replication,'entries_for',return_value=[]),patch.object(q.b.tools,'authorize'), \
             patch.object(q.b,'stream_transfer',side_effect=stream):
            directory=Path(raw).resolve()/'intent'
            if changed_inventory:
                with self.assertRaises(ValueError):q.run(policy,catalog,lock,a,'private',directory,api)
                self.assertEqual(transfers,[]);return
            result=q.run(policy,catalog,lock,a,'private',directory,api)
        self.assertTrue(result['complete']);self.assertTrue(result['helper_pods_absent'])
        self.assertEqual(len(result['retained_inventory']['acknowledged_receipts']),18)
        self.assertEqual(transfers,list(range(18,36)))
        self.assertEqual(result['targets']['mo-standard-1']['verify'],one)
        self.assertEqual([x['kind'] for x in made],['ConfigMap','Pod','Pod','Pod'])
        self.assertEqual(len(result['retired_helpers']),3);self.assertEqual(len(result['deleted']),3)
        self.assertEqual(sum(x['transferred_bytes'] for x in result['targets']['mo-standard-2']['batches'][18:]),42-19)
        self.assertGreater(len(checks),18)
        for i,pod in enumerate(made[1:]):
            spec=pod['spec'];self.assertEqual(spec['activeDeadlineSeconds'],9000)
            self.assertFalse(spec['automountServiceAccountToken'])
            self.assertEqual(spec['volumes'][0]['persistentVolumeClaim'].get('readOnly',False),i<2)
            self.assertEqual(spec['containers'][0]['volumeMounts'][0].get('readOnly',False),i<2)
    def test_full_ruh_resume_no_new_pvc_skip_acks_and_only_missing(self):self.exercise()
    def test_retained_inventory_change_prevents_transfer(self):self.exercise(changed_inventory=True)


if __name__=='__main__':unittest.main()
