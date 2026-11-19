import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import node_cache_native_pvc_catalog as cat
import node_cache_native_pvc_authority as auth
import build_native_measured_real_pr_workflow as measured

CFG=Path(__file__).resolve().parents[1]/'config'

class Supplements(unittest.TestCase):
    def test_all_exact_volumes_and_tamper(self):
        with patch.object(auth,'ISSUER',('test',1)):
            merged,_=cat.load(CFG)
            self.assertEqual(len(merged['nodes']),12);self.assertEqual(len(merged['entries']),18)
            for env in ('mnl2','jed','ruh'):
                d=auth.document(CFG,env);auth.validate(d,CFG)
                for change in ('id','runtime','volume','parent'):
                    bad=copy.deepcopy(d)
                    if change=='id':bad['supplement_sha256']='0'*64
                    if change=='runtime':bad['additional_entry']['runtime']['environment']='unknown'
                    if change=='volume':bad['base_catalog']['nodes'].pop()
                    if change=='parent':bad['parent_authorization_id']='0'*64
                    with self.assertRaises(ValueError):auth.validate(bad,CFG)
                for b in d['base_catalog']['nodes']:
                    local=cat.cache.catalog_for_node(merged,b)
                    actual=cat.cache.native_authorities(merged,b,local['entries'])
                    key,manifest=next(iter(d['additional_entry']['native_manifests'].items()))
                    self.assertEqual(actual[key],manifest)
        with patch.object(auth,'ISSUER',None),self.assertRaises(ValueError):cat.load(CFG)

    def test_workflow_preserves_original_after_normalization(self):
        prev=measured.parent.build();cur=measured.build()
        cur['name']=prev['name']
        for job in cur['jobs'].values():
            for step in job.get('steps',[]):
                if 'run' in step:step['run']=step['run'].replace('node_cache_native_pvc_catalog.py','node_cache_pvc_authority.py').replace(
                    'run_measured_coverage_packages.py','run_coverage_packages.py')
                if step.get('id')=='upload_coverage_report':
                    step['with']['path']=step['with']['path'].removesuffix('${{ runner.temp }}/go-rebuild-counts.json\n')
        self.assertEqual(cur,prev)
        self.assertEqual((CFG.parent/measured.TARGET).read_text(),measured.render())
