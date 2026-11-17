import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coverage_two_groups import plan, FILE_SERVICE, EMBED, ROOT
import verify_two_group_producer as v


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        packages = [FILE_SERVICE, EMBED] + [ROOT+f'pkg/fixture{i}' for i in range(503)]
        self.manifest = {'source_sha':v.SOURCE,'storage_mode':'cs-s3','started_packages':packages,
            'failed':[],'package_timeouts':[],'malformed_events':0,
            'skipped_tests':[{'package':packages[0],'test':str(i),'reasons':[]} for i in range(86)],
            'packages_without_tests':[{'package':p,'test':'','reasons':[]} for p in packages[2:4]]}
        for arm in ('baseline','candidate'):
            self.put(self.root/arm/'skips/combined-skip-manifest.json', self.manifest)
        partition = plan(packages,2)
        for group in range(2):
            fp = {'source':v.SOURCE,'cs_environment':'mty','group':str(group),'run':'2','attempt':'1',
                  'tools':{'go':'version'}, 'cpu':{'model':'fixed'}, 'workspace':'/fixed'}
            old = dict(fp,policy='old-tested-policy',run='1')
            self.put(self.root/'baseline/reports'/f'baseline-ut-coverage-report-group-{group}-attempt-1/baseline-environment.json',
                {'fingerprint':old,'restored_cache':False,'seed_precompile':False})
            fp.update(group_count=2,policy=hashlib.sha256(Path(v.__file__).with_name('coverage_build_cache.py').read_bytes()).hexdigest(),
                      partition_policy=hashlib.sha256(Path(v.__file__).with_name('coverage_two_groups.py').read_bytes()).hexdigest())
            self.put(self.root/'candidate/reports'/f'cold-ut-coverage-report-group-{group}-attempt-1/build-cache-artifact/manifest.json',
                {'compile_only':True,'fingerprint':fp,'coverage_contract':{
                    'full_packages':packages,'coverpkg':packages[:484],'packages':partition['groups'][group]}})

    def put(self,path,obj):
        path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(obj))

    def verify(self):
        with patch.object(v,'profile',return_value={('fixed', '1')}):
            return v.verify(self.root)

    def test_complete_two_group_scope(self):
        self.assertEqual(self.verify()['packages'],505)

    def test_changed_skip_rejected(self):
        x=copy.deepcopy(self.manifest); x['skipped_tests'][0]['reasons']=['different']
        self.put(self.root/'candidate/skips/combined-skip-manifest.json',x)
        with self.assertRaises(AssertionError):self.verify()

    def test_bad_policy_scope_and_hardware_rejected(self):
        path=self.root/'candidate/reports/cold-ut-coverage-report-group-0-attempt-1/build-cache-artifact/manifest.json'
        original=json.loads(path.read_text())
        for key,value in [('group_count',3),('partition_policy','wrong'),('policy','wrong'),('tools',{'go':'changed'})]:
            with self.subTest(key=key):
                x=copy.deepcopy(original);x['fingerprint'][key]=value;self.put(path,x)
                with self.assertRaises(AssertionError):self.verify()
        x=copy.deepcopy(original);x['coverage_contract']['packages'].pop();self.put(path,x)
        with self.assertRaises(AssertionError):self.verify()

    def test_extra_or_missing_report_rejected(self):
        self.put(self.root/'candidate/reports/cold-ut-coverage-report-group-2-attempt-1/extra.json',{})
        with self.assertRaises(AssertionError):self.verify()

    def test_profile_mode_refused(self):
        p=self.root/'bad.out';p.write_text('mode: atomic\n')
        with self.assertRaises(AssertionError):v.profile(p)


if __name__=='__main__':unittest.main()
