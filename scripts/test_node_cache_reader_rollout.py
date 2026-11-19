import copy
import datetime as dt
from pathlib import Path
import unittest
from unittest.mock import patch

import node_cache_reader_rollout as p
import test_node_cache_demand_plan as fixture
import test_node_cache_workspace_probe_control as probe


class ReaderRollout(unittest.TestCase):
    def fixture(self,env='mnl2'):
        cat,policy,old,sc=fixture.AllNodePlan().fixture(env)
        old['spec']['listenerTemplate']={'spec':{'containers':[{'name':'listener'}]}}
        cpus={n:next(e['runtime']['cpu'] for e in p.cache.catalog_for_node(cat,
            next(b for b in cat['nodes'] if (b['environment'],b['node'])==(env,n)))['entries']) for n in p.plan.NODES}
        for e in cat['entries']:
            e['expires_at']='2099-01-01T00:00:00Z';e['native_manifests']={'a'*64:'b'*64}
        return cat,policy,old,sc,cpus

    def test_all_environments_three_nodes_no_count_cap_and_same_private_contract(self):
        for env in p.plan.ENVIRONMENTS:
            values=self.fixture(env);before=copy.deepcopy(values)
            cat,policy,old,sc,cpus=values
            result=p.resources(cat,policy,old,sc,list(p.plan.NODES),cpus)
            self.assertEqual(values,before);self.assertEqual(result['pending_nodes'],[])
            self.assertEqual(len(result['resources']),3)
            for r in result['resources']:
                self.assertNotIn('minRunners',r['spec']);self.assertNotIn('maxRunners',r['spec'])
                self.assertEqual(r['spec']['listenerTemplate']['spec']['containers'][0]['resources'],p.capacity.LISTENER_RESOURCES)
                self.assertEqual(r['spec']['template']['spec']['containers'][0]['resources']['limits']['memory'],'14Gi')

    def test_expired_generation_excluded_not_renewed_when_valid_delta_exists(self):
        root=Path(__file__).resolve().parents[1]/'config'
        cat=p.c.read_json(root/'node-compile-cache-catalog.json');before=copy.deepcopy(cat)
        binding=next(b for b in cat['nodes'] if (b['environment'],b['node'])==('mnl2','mo-standard-0'))
        cpu=next(e['runtime']['cpu'] for e in cat['entries'] if e['runtime']['environment']=='mnl2')
        valid,expired=p.available_entries(cat,binding,cpu,dt.datetime(2026,11,19,tzinfo=dt.timezone.utc))
        self.assertEqual(len(valid),2);self.assertEqual(len(expired),1);self.assertEqual(cat,before)
        with self.assertRaises(ValueError):p.available_entries(cat,binding,cpu,dt.datetime(2099,1,1,tzinfo=dt.timezone.utc))
        with self.assertRaises(ValueError):p.available_entries(cat,binding,{'wrong':'cpu'})

    def test_full_independent_cpu_plan_binding_and_primary_api(self):
        root=Path(__file__).resolve().parents[1]/'config'
        plan=p.c.read_json(root/'node-cache-reviewed-cpu-plan.json');base=p.c.read_json(root/'node-compile-cache-catalog.json')
        for env,(runid,sha,artifactid,digest) in p.CPU_AUTHORITIES.items():
            run={'id':runid,'head_sha':sha,'run_attempt':1,'status':'completed','conclusion':'success','event':'workflow_dispatch',
                'head_branch':'codex/ruh-pvc-readiness','actor':{'login':'Ariznawlll'},'triggering_actor':{'login':'Ariznawlll'},
                'path':'.github/workflows/cloudsigma-up.yaml','display_title':f'up cs-component / {env} / node-cache-workspace-probe'}
            artifact={'id':artifactid,'digest':digest,'expired':False,'workflow_run':{'id':runid,'head_sha':sha}}
            def api(path):return run if '/runs/' in path else artifact
            self.assertEqual(set(p.cpu_inventory(plan,base,env,api)),set(p.plan.NODES))
            bad=copy.deepcopy(plan);bad['observations'][0]['cpu']['flags']='changed'
            with self.assertRaises(ValueError):p.cpu_inventory(bad,base,env,api)
            artifact['expired']=True
            with self.assertRaises(ValueError):p.cpu_inventory(plan,base,env,api)

    def snapshots(self,policy):
        sample_policy,snap=probe.ProbeChecks().snapshot();snaps={}
        policy['namespace_uids']=sample_policy['namespace_uids']
        nodes=[]
        for name in p.plan.NODES:
            row=copy.deepcopy(snap['nodes']['items'][0]);row['metadata'].update(name=name,uid=policy['node_uids'][name])
            row['status']['allocatable']={'cpu':'64','memory':'256Gi'};nodes.append(row)
        for name in p.plan.NODES:
            local=copy.deepcopy(snap);local['nodes']['items']=copy.deepcopy(nodes)
            local['stats']['node']['memory']['availableBytes']=120*p.c.GIB
            local['drives']['items'][0]['metadata']['labels']['directpv.min.io/node']=name
            snaps[name]=local
        return snaps

    def test_copy_transport_and_only_unexpired_exact_cpu_producers_authenticated(self):
        cat,policy,old,sc,cpus=self.fixture('mnl2')
        selected=next(e for e in cat['entries'] if e['runtime']['environment']=='mnl2')
        expired=copy.deepcopy(selected);expired['expires_at']='2000-01-01T00:00:00Z';expired['index_sha256']='e'*64
        cat['entries'].append(expired)
        lock={'producer':{'repository':'matrixorigin/CI','id':7},'artifact':{'id':8}}
        row={'environment':'mnl2','run_id':17,'gitops_sha':'a'*40,'artifact_id':99,'artifact_digest':'sha256:'+'b'*64}
        evidence={'schema':1,'catalog_canonical_sha256':p.cache.hash_value(cat),'copies':[row]}
        run={'id':17,'head_sha':'a'*40,'conclusion':'success','status':'completed','event':'workflow_dispatch',
            'run_attempt':1,'head_branch':'codex/ruh-pvc-readiness','path':'.github/workflows/cloudsigma-up.yaml',
            'actor':{'login':'Ariznawlll'},'triggering_actor':{'login':'Ariznawlll'},
            'display_title':'up cs-component / mnl2 / node-cache-replicate'}
        artifact={'digest':row['artifact_digest'],'expired':False,'workflow_run':{'id':17,'head_sha':'a'*40}}
        for fault in (None,'copy','producer','expired'):
            r,a=copy.deepcopy((run,artifact))
            if fault=='copy':r['head_sha']='c'*40
            elif fault=='expired':a['expired']=True
            def api(path):return r if path.endswith('/runs/17') else a if path.endswith('/artifacts/99') else {}
            with patch.object(p.cache,'reviewed_entries',side_effect=lambda rows,*args:[] if fault=='producer' else rows['entries']) as reviewed, \
                    patch.object(p.q.base.tools,'authorize'):
                if fault:
                    with self.assertRaises(ValueError):p.authenticate(cat,lock,evidence,'mnl2',list(p.plan.NODES),cpus,api)
                else:
                    proof=p.authenticate(cat,lock,evidence,'mnl2',list(p.plan.NODES),cpus,api)
                    self.assertTrue(all(expired['index_sha256'] in indexes for indexes in proof['expired_indexes_not_renewed_or_used'].values()))
                    self.assertTrue(all(expired not in call.args[0]['entries'] for call in reviewed.call_args_list))

    def test_registration_keeps_node2_target_but_private_claim_shortage_waits(self):
        cat,policy,old,sc,cpus=self.fixture('mty');snaps=self.snapshots(policy)
        resources=p.resources(cat,policy,old,sc,list(p.plan.NODES),cpus)['resources']
        # Register all three listeners. Initial jobs only on nodes0/1; no
        # authored limit prevents later jobs from waiting for real storage.
        snaps['mo-standard-2']['drives']['items'][0]['status']['freeCapacity']=100*p.c.GIB-1
        result=p.admission(policy,resources,snaps,job_nodes=['mo-standard-0','mo-standard-1'])
        self.assertEqual(result['registered_nodes'],list(p.plan.NODES))
        self.assertIsNone(result['runner_count_cap'])
        with self.assertRaisesRegex(ValueError,'private workspace capacity'):
            p.admission(policy,resources,snaps,job_nodes=['mo-standard-2'])
        snaps['mo-standard-2']['drives']['items'][0]['status']['freeCapacity']+=1
        p.admission(policy,resources,snaps,job_nodes=['mo-standard-2'])

    def test_memory_root_actual_declared_quota_and_cap_not_waived(self):
        for fault in ('memory','root','declared','quota','cap'):
            cat,policy,old,sc,cpus=self.fixture('mty');snaps=self.snapshots(policy)
            resources=p.resources(cat,policy,old,sc,list(p.plan.NODES),cpus)['resources']
            if fault=='memory':snaps['mo-standard-1']['stats']['node']['memory']['availableBytes']=17.9*p.c.GIB
            elif fault=='root':snaps['mo-standard-0']['stats']['node']['fs']['availableBytes']=59*p.c.GIB
            elif fault=='declared':snaps['mo-standard-1']['nodes']['items'][1]['status']['allocatable']['memory']='16Gi'
            elif fault=='quota':snaps['mo-standard-0']['quotas']['items']=[{'status':{'hard':{'requests.storage':'159Gi'},'used':{}}}]
            elif fault=='cap':resources[0]['spec']['maxRunners']=1
            with self.subTest(fault=fault),self.assertRaises(ValueError):
                p.admission(policy,resources,snaps,job_nodes=['mo-standard-0','mo-standard-1'])


if __name__=='__main__':unittest.main()
