"""Exercise ONLY the files delivered by the generated consumer checkout.

No network, Go build, cluster, PVC or PR mutation. -I -S excludes the author's
PYTHONPATH/site-packages, so a missing transitive helper cannot pass locally.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import build_pr_node_cache_workflow as consumer
import build_real_pr_node_cache_workflow as actual_pr

CODE=r'''
import json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root/'scripts'))
import node_cache_pr_source
import node_cache_pr_catalog as catalog
import node_cache_pr_native_catalog as native
values=[json.loads((root/'config'/name).read_text()) for name in (
    'node-compile-cache-catalog.json','node-cache-all-node-copy-evidence.json',
    'node-compile-cache-pr-catalog.json','node-cache-pr-profile-proof.json')]
result=catalog.validate(*values)
result['native_additions']=native.load(root/'config')[1]['native_additions']
print(json.dumps(result))
'''


class SparseRuntime(unittest.TestCase):
    def step(self,workflow):
        return next(s for s in workflow['jobs']['ut-coverage-linux-x86']['steps']
            if s.get('name')=='Checkout CI coverage helper')['with']

    def materialize(self,path,workflow):
        step=self.step(workflow)
        self.assertEqual(step['sparse-checkout'].splitlines(),['scripts','config'])
        self.assertIs(step['sparse-checkout-cone-mode'],True)
        self.assertEqual(step['repository'],'${{ job.workflow_repository }}')
        self.assertEqual(step['ref'],'${{ job.workflow_sha }}')
        for name in step['sparse-checkout'].splitlines():
            shutil.copytree(consumer.ROOT/name,path/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))

    def run_isolated(self,path):
        return subprocess.run([sys.executable,'-I','-S','-c',CODE,str(path)],cwd=path,
            capture_output=True,text=True,timeout=30)

    def test_both_generated_workflows_run_catalog_with_only_sparse_contents(self):
        for workflow in (consumer.build(),actual_pr.build()):
            with self.subTest(workflow=workflow['name']),tempfile.TemporaryDirectory(prefix='pr-sparse-contract-') as tmp:
                path=Path(tmp);self.materialize(path,workflow);result=self.run_isolated(path)
                self.assertEqual(result.returncode,0,result.stderr)
                proof=json.loads(result.stdout)
                self.assertTrue(proof['old_copy_authority_preserved']);self.assertFalse(proof['live_pr_ut_accepted'])
                self.assertEqual(proof['native_additions'],2)

    def test_missing_provenance_transitive_helper_config_or_mutated_proof_fails(self):
        for fault in ('scripts/node_cache_pr_source.py','scripts/node_cache_profile_receipt.py',
                'config/node-cache-all-node-copy-evidence.json','scripts/node_cache_pr_native_catalog.py',
                'config/node-native-publication-n1-receipt.json','changed-proof'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory(prefix='pr-sparse-negative-') as tmp:
                path=Path(tmp);self.materialize(path,consumer.build())
                if fault=='changed-proof':
                    target=path/'config/node-cache-pr-profile-proof.json';value=json.loads(target.read_text())
                    value['base_catalog_sha256']='0'*64;target.write_text(json.dumps(value))
                else:(path/fault).unlink()
                self.assertNotEqual(self.run_isolated(path).returncode,0)


if __name__=='__main__':unittest.main()
