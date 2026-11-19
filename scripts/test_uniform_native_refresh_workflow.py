import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
import build_uniform_native_refresh_workflow as build
import node_cache_native_refresh as native
import node_compile_cache as cache


class UniformNativeContract(unittest.TestCase):
    def test_real_bootstrap_binding_guards_execute_for_each_environment_and_reject_wrong_bindings(self):
        job=build.build()['jobs']['native-only'];step=job['steps'][0]
        guard=step['run'].split('started=$SECONDS',1)[0]
        self.assertNotIn('= mty',guard)
        for target in ('jed','mnl2','ruh'):
            shell=guard.replace('${{ inputs.environment }}',target).replace('${{ matrix.node }}','mo-standard-0')
            for fault in (None,'env','node','expected_node'):
                env={'PATH':os.environ.get('PATH',''),'CS_CACHE_ENV':target,'CS_CACHE_NODE':'mo-standard-0','CS_CACHE_EXPECTED_NODE':'mo-standard-0'}
                if fault=='env':env['CS_CACHE_ENV']='mty'
                elif fault=='node':env['CS_CACHE_NODE']='mo-standard-1'
                elif fault=='expected_node':env['CS_CACHE_EXPECTED_NODE']='mo-standard-1'
                result=subprocess.run(['/bin/bash','-c',shell],env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode==0,fault is None,(target,fault))

    def test_frozen_one_node_producer_keeps_no_ut_no_go_precompile_and_readonly_restore(self):
        value=build.build();job=value['jobs']['native-only']
        self.assertEqual(job['strategy'],{'fail-fast':False,'matrix':{'node':['mo-standard-0']}})
        self.assertEqual(job['runs-on'],"${{ format('cs-{0}-nc-n0-pr-v1', inputs.environment) }}")
        scripts='\n'.join(s.get('run','') for s in job['steps'])
        self.assertNotIn('run_coverage_packages',scripts);self.assertNotIn('go test',scripts)
        self.assertIn('node_cache_pvc_authority.py materialize',scripts)
        self.assertIn('node_cache_native_refresh.py freeze',scripts)
        self.assertIn('cs-reviewed-persistent-catalog.json',scripts)
        self.assertNotIn('publish_node_cache',scripts)
        path=Path(__file__).resolve().parents[1]/build.TARGET
        self.assertEqual(path.read_text(),build.render())

    def test_exact_current_source_uniform_cpu_requests_do_not_allow_other_env_node_or_key(self):
        root=Path(__file__).resolve().parents[1]/'config'
        expected={'jed':'3bbdd1badc8f54acf3dbb8b025beb9220c4215fabc7392796297ccbc0a1da20c',
            'mnl2':'d63345e3ba444e31b4d7eba0723ff34f4bcfd387d901840a0b84f3a7a5889342',
            'ruh':'11e5069c8816d65f2c8311eff59cf7744e78817e16d18193a32923e09e7987d5'}
        for env,digest in expected.items():
            request=json.loads((root/f'node-native-uniform-request-{env}.json').read_text())
            self.assertEqual(cache.hash_value(request),digest)
            self.assertEqual(native.profile_scope(request,'mo-standard-0'),env)
            with self.assertRaises(ValueError):native.profile_scope(request,'mo-standard-1')
            bad=copy.deepcopy(request);bad['target_cpu_sha256']['mo-standard-2']='0'*64
            with self.assertRaises(ValueError):native.profile_scope(bad,'mo-standard-0')
            bad=copy.deepcopy(request);bad['current_pr_head_sha']='a'*40
            with self.assertRaises(ValueError):native.profile_scope(bad,'mo-standard-0')


if __name__=='__main__':unittest.main()
