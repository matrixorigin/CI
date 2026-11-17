import json
from pathlib import Path
import tempfile
import unittest

from coverage_two_groups import FILE_SERVICE, EMBED, ROOT, plan
from coverage_two_groups_merge import validate


class GroupCountTest(unittest.TestCase):
    def scope(self):
        return [FILE_SERVICE, EMBED] + [ROOT + f'pkg/fixture{i}' for i in range(503)]

    def test_two_complete_disjoint_deterministic(self):
        packages=self.scope();result=plan(packages,2)
        self.assertEqual(result,plan(list(reversed(packages)),2))
        self.assertEqual(len(result['groups']),2)
        self.assertEqual(set().union(*map(set,result['groups'])),set(packages))
        self.assertEqual(sum(map(len,result['groups'])),505)
        self.assertIn(FILE_SERVICE,result['groups'][0])
        self.assertIn(EMBED,result['groups'][1])

    def test_default_stays_three(self):
        self.assertEqual(plan(self.scope()),plan(self.scope(),3))
        self.assertEqual(len(plan(self.scope())['groups']),3)

    def test_invalid_count_rejected(self):
        for count in (0,1,4,True,2.0,'2'):
            with self.subTest(count=count),self.assertRaises(ValueError):plan(self.scope(),count)

    def fixture(self,root,count):
        p=plan(self.scope(),count)
        for group in range(count):
            folder=root/str(group);folder.mkdir()
            (folder/'coverage-group-plan.json').write_text(json.dumps(dict(p,selected_group=group)))
            (folder/'ut-coverage-skip-manifest.json').write_text(json.dumps({'producer_status':'passed','malformed_events':0,'failed':[],'package_timeouts':[],'source_sha':'fixed','started_packages':p['groups'][group]}))

    def test_two_merge_success_and_missing_group_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root,2)
            self.assertEqual(len(validate(root,'fixed',2)),2)
            with self.assertRaises(ValueError):validate(root,'wrong',2)
            (root/'1/ut-coverage-skip-manifest.json').unlink()
            with self.assertRaises(FileNotFoundError):validate(root,'fixed',2)

    def test_three_group_evidence_cannot_pass_two_group_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root,3)
            with self.assertRaises(ValueError):validate(root,'fixed',2)

    def test_failed_or_missing_package_rejected(self):
        for field,value in [('failed',['fixture']),('package_timeouts',['fixture']),('started_packages',[])]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.fixture(root,2)
                path=root/'0/ut-coverage-skip-manifest.json';r=json.loads(path.read_text());r[field]=value;path.write_text(json.dumps(r))
                with self.assertRaises(ValueError):validate(root,'fixed',2)

if __name__=='__main__':unittest.main()
