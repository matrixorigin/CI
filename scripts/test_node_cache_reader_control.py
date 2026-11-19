import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import node_cache_reader_control as control
import test_node_cache_reader_rollout as f
import test_node_cache_cpu_producer_fleet as cpu_fixture


class ReaderController(unittest.TestCase):
    def test_listener_ready_requires_fresh_same_uid_scale_id_not_stale_initial_get(self):
        resource,live,listener,pod=cpu_fixture.ProducerFleet().listener()
        row={'name':resource['metadata']['name'],'uid':live['metadata']['uid']}
        stale=copy.deepcopy(live);stale['metadata']['annotations']={}
        class Fake:
            def __init__(self):self.set_reads=0
            def call(self,args):
                if args[1]=='autoscalingrunnerset':
                    self.set_reads+=1
                    return copy.deepcopy(stale if self.set_reads==1 else live)
                if args[1]=='autoscalinglisteners':return {'items':[listener]}
                if args[1]=='pods':return {'items':[pod]}
                raise AssertionError(args)
        session=Fake();result=control.observe_listener(session,resource,row)
        self.assertEqual(result['scale_set_id'],101);self.assertEqual(session.set_reads,2)
        live['metadata']['uid']='replaced'
        with self.assertRaises(ValueError):control.observe_listener(Fake(),resource,row)

    def execute(self,fault=None):
        helper=f.ReaderRollout();cat,policy,old,sc,cpus=helper.fixture('jed')
        snaps=helper.snapshots(policy)
        policy['namespace_uids']={k.replace('mty','jed'):v for k,v in policy['namespace_uids'].items()}
        for s in snaps.values():
            for n in s['namespaces']['items']:n['metadata']['name']=n['metadata']['name'].replace('mty','jed')
        old['metadata']['uid']='original-pool';live={};mutations=[]
        class Fake(control.Session):
            def snapshot(self,node):
                result=copy.deepcopy(snaps[node])
                for k in ('cpu','memory','fs'):result['stats']['node'][k]['time']=control.q.pub.now().isoformat()
                return result
            def cache_volume(self,binding):return binding
            def call(self,args,data=None,timeout=45):
                verb,kind=args[:2]
                if verb=='get' and kind=='autoscalingrunnerset':
                    name=args[2]
                    if name==old['metadata']['name']:return copy.deepcopy(old)
                    return copy.deepcopy(live.get(name))
                if verb=='get' and kind=='autoscalingrunnersets':return {'items':[old,*live.values()]}
                if verb=='create':
                    name=data['metadata']['name'];mutations.append((verb,data['kind'],name))
                    live[name]=copy.deepcopy(data);live[name]['metadata']['uid']=name+'-uid'
                    if fault=='unknown':raise TimeoutError('response unknown after create')
                    return copy.deepcopy(live[name])
                raise AssertionError(args)
        def listener(session,resource,row):
            if fault=='listener':raise ValueError('listener identity changed')
            return {'listener_pod_uid':row['uid']+'-listener'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(control,'Session',Fake), \
                patch.object(control,'observe_listener',side_effect=listener), \
                patch.object(control.fleet,'cpu_inventory',return_value=cpus), \
                patch.object(control.fleet,'authenticate',side_effect=ValueError('producer') if fault=='authority' else None,return_value={}):
            directory=Path(tmp).resolve()/'intent'
            if fault:
                with self.assertRaises((ValueError,TimeoutError)):
                    control.run(policy,cat,{}, {},{},'fixture',directory,lambda path:{})
                if fault=='authority':self.assertFalse(directory.exists());self.assertEqual(mutations,[])
                else:
                    state=control.c.read_json(directory/'state.json');self.assertFalse(state['complete'])
                    self.assertEqual(len(mutations),1)
                    self.assertEqual('pending_create' in state,fault=='unknown')
                    with self.assertRaisesRegex(ValueError,'intent exists'):
                        control.run(policy,cat,{}, {},{},'fixture',directory,lambda path:{})
            else:
                state=control.run(policy,cat,{}, {},{},'fixture',directory,lambda path:{})
                self.assertTrue(state['complete']);self.assertEqual(len(state['created']),3)
                self.assertEqual(state['registered_nodes'],list(control.fleet.plan.NODES))
                self.assertEqual(state['original_pool']['uid'],'original-pool')
                self.assertTrue(all(v[:2]==('create','AutoscalingRunnerSet') for v in mutations))
                self.assertEqual(len(mutations),3)
                self.assertIsNone(state['runner_count_cap']);self.assertFalse(state['workflow_dispatched'])

    def test_three_new_sets_only_original_pool_and_cache_not_changed(self):self.execute()
    def test_authority_before_mutation_and_unknown_response_never_retried(self):
        for fault in ('authority','unknown','listener'):
            with self.subTest(fault=fault):self.execute(fault)


if __name__=='__main__':unittest.main()
