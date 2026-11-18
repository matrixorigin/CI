"""Real fresh-directory lifecycle: no PR identity check may pre-create cache.

Runs actual git identity, configure() and immutable provenance writes in ONLY
private temporary fixtures. Platform/native probing and disk capacity are stubbed;
the production fresh-directory check, actual Cache constructor and JSON I/O are not.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import build_pr_node_cache_workflow as consumer
import build_real_pr_node_cache_workflow as actual_pr
import incremental_cache as c
import node_compile_cache as cache
import node_cache_pr_source as source


class FreshLifecycle(unittest.TestCase):
    def fixture(self,tmp):
        root=Path(tmp).resolve();checkout=root/'checkout';temp=root/'temp';checkout.mkdir();temp.mkdir()
        subprocess.run(['git','init','-q',str(checkout)],check=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            'commit','--allow-empty','-qm','fixture'],cwd=checkout,check=True)
        head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout,text=True).strip()
        catalog=root/'catalog.json';catalog.write_text(json.dumps({'schema':2,'nodes':[],'entries':[]}))
        return root,checkout,temp,head,catalog

    def configure(self,root,checkout,temp,catalog):
        with mock.patch.object(cache,'runtime',return_value={'repository':'matrixorigin/matrixone','environment':'mty'}), \
                mock.patch.object(cache,'native_key',return_value=('a'*64,{'Makefile':'b'*64})), \
                mock.patch.object(cache.shutil,'disk_usage',return_value=SimpleNamespace(free=100*c.GIB)), \
                mock.patch.dict(os.environ,{'GITHUB_ENV':str(root/'github-env')},clear=False):
            return cache.configure(catalog,root/'shared-absent',checkout,temp/'cs-node-cache','mty','matrixorigin/matrixone')

    def test_check_configure_record_preserves_actual_exclusive_fresh_creation(self):
        with tempfile.TemporaryDirectory(prefix='pr-source-lifecycle-') as tmp:
            root,checkout,temp,head,catalog=self.fixture(tmp);directory=temp/'cs-node-cache'
            expected=source.provenance('check',checkout,temp,'fixture/matrixone',head,'matrixorigin/matrixone')
            self.assertFalse(directory.exists())
            self.configure(root,checkout,temp,catalog)
            self.assertTrue((directory/'config.json').is_file());self.assertTrue((directory/'state.json').is_file())
            actual=source.provenance('record',checkout,temp,'fixture/matrixone',head,'matrixorigin/matrixone')
            self.assertEqual(expected,actual);self.assertEqual(c.read_json(directory/'pr-source.json'),actual)
            with self.assertRaisesRegex(ValueError,'per-job cache must be fresh'):self.configure(root,checkout,temp,catalog)
            with self.assertRaises(ValueError):source.provenance('record',checkout,temp,'fixture/matrixone',head,'matrixorigin/matrixone')

    def test_moved_head_missing_config_or_different_config_source_never_records(self):
        with tempfile.TemporaryDirectory(prefix='pr-source-negative-') as tmp:
            root,checkout,temp,head,catalog=self.fixture(tmp);directory=temp/'cs-node-cache'
            with self.assertRaises(ValueError):source.provenance('check',checkout,temp,'fixture/matrixone','0'*40,'matrixorigin/matrixone')
            self.assertFalse(directory.exists())
            with self.assertRaises(FileNotFoundError):source.provenance('record',checkout,temp,'fixture/matrixone',head,'matrixorigin/matrixone')
            self.assertFalse(directory.exists())
            self.configure(root,checkout,temp,catalog)
            other=root/'other-checkout'
            subprocess.run(['git','clone','-q',str(checkout),str(other)],check=True)
            with self.assertRaises(ValueError):source.provenance('record',other,temp,'fixture/matrixone',head,'matrixorigin/matrixone')
            self.assertFalse((directory/'pr-source.json').exists())

    def test_legacy_record_first_fails_and_both_generated_orders_are_safe(self):
        for workflow in (consumer.build(),actual_pr.build()):
            step=next(s for s in workflow['jobs']['ut-coverage-linux-x86']['steps'] if s.get('id')=='node_cache')
            text=step['run']
            self.assertLess(text.index('node_cache_pr_source.py check'),text.index('node_compile_cache.py configure'))
            self.assertLess(text.index('node_compile_cache.py configure'),text.index('node_cache_pr_source.py record'))
        with tempfile.TemporaryDirectory(prefix='pr-source-old-order-') as tmp:
            root,checkout,temp,head,catalog=self.fixture(tmp)
            c.atomic_json(temp/'cs-node-cache/pr-source.json',source.checked('fixture/matrixone',head,head))
            with self.assertRaisesRegex(ValueError,'per-job cache must be fresh'):self.configure(root,checkout,temp,catalog)


if __name__=='__main__':unittest.main()
