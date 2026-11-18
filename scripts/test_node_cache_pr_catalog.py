import copy
from pathlib import Path
import unittest

import incremental_cache as c
import node_cache_pr_catalog as p
import node_cache_pr_source as source
import test_node_cache_catalog as structures


class ReaderCatalog(unittest.TestCase):
    def fixture(self):
        root=Path(__file__).resolve().parents[1]/'config'
        return [c.read_json(root/name) for name in ('node-compile-cache-catalog.json',
            'node-cache-all-node-copy-evidence.json','node-compile-cache-pr-catalog.json','node-cache-pr-profile-proof.json')]

    def test_reviewed_extension_preserves_original_copy_evidence_and_all_other_nodes(self):
        values=self.fixture();before=copy.deepcopy(values)
        result=p.validate(*values)
        self.assertEqual(values,before);self.assertTrue(result['old_copy_authority_preserved'])
        self.assertTrue(result['other_environments_unchanged']);self.assertFalse(result['live_pr_ut_accepted'])
        self.assertEqual(len(values[2]['nodes']),12)
        structures.check(values[2])
        self.assertEqual(values[2]['entries'][-1]['bindings'],[{
            k:values[3]['append_approval']['base']['binding'][k] for k in ('node_uid','pvc_uid','pv_uid')}])

    def test_wrong_authority_mutated_old_entry_marker_new_cpu_or_raw_receipt_refused(self):
        for fault in ('copy','expiry','old-node','controller','artifact','raw','new-binding','new-cpu','extra'):
            base,copied,active,proof=self.fixture()
            if fault=='copy':copied['catalog_canonical_sha256']='0'*64
            if fault=='expiry':active['entries'][1]['expires_at']='2099-01-01T00:00:00Z'
            if fault=='old-node':active['nodes'][0]['marker_sha256']='0'*64
            if fault=='controller':proof['append_approval']['controller_run']['head_sha']='a'*40
            if fault=='artifact':proof['append_approval']['artifact']['digest']='sha256:'+'0'*64
            if fault=='raw':proof['raw_receipt']+=' '
            if fault=='new-binding':active['entries'][-1]['bindings'][0]['pvc_uid']='other'
            if fault=='new-cpu':active['entries'][-1]['runtime']['cpu']['model']='alias'
            if fault=='extra':active['entries'].append(copy.deepcopy(active['entries'][-1]))
            with self.subTest(fault=fault),self.assertRaises(ValueError):p.validate(base,copied,active,proof)

    def test_trusted_fork_identity_kept_separate_from_project_cache_namespace(self):
        sha='a'*40
        for repository in ('matrixorigin/matrixone','trusted-user/matrixone'):
            value=source.checked(repository,sha,sha)
            self.assertEqual(value['actual_pr_repository'],repository)
            self.assertEqual(value['project_cache_namespace'],'matrixorigin/matrixone')
            self.assertEqual(value['actual_pr_head_sha'],sha)
        for args in (('other;command',sha,sha),('trusted-user/matrixone',sha,'b'*40),
                     ('trusted-user/matrixone',sha,sha,'other/project')):
            with self.subTest(args=args),self.assertRaises(ValueError):source.checked(*args)


if __name__=='__main__':unittest.main()
