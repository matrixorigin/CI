"""Immutable publication proofs and the real fresh-directory consumer lifecycle."""
import copy
from pathlib import Path
import tempfile
import unittest

import incremental_cache as c
import node_cache_pr_native_catalog as native
import node_cache_pr_source as source
from test_node_cache_pr_source_lifecycle import FreshLifecycle

ROOT=Path(__file__).resolve().parents[1]/'config'


class NativeCatalog(unittest.TestCase):
    def values(self):
        values=[c.read_json(ROOT/name) for name in ('node-compile-cache-catalog.json',
            'node-cache-all-node-copy-evidence.json','node-compile-cache-pr-catalog.json',
            'node-cache-pr-profile-proof.json','node-native-refresh-request.json')]
        values.append([{'approval':c.read_json(ROOT/f'node-native-publication-n{i}.json'),
            'report':c.read_json(ROOT/f'node-native-publication-n{i}-receipt.json')} for i in (0,1)])
        return values

    def test_exact_audited_additions_preserve_all_existing_authority(self):
        values=self.values();before=copy.deepcopy(values)
        catalog,proof=native.compose(*values)
        self.assertEqual(values,before)
        self.assertEqual(catalog['entries'][:13],values[2]['entries'])
        self.assertEqual(catalog['nodes'],values[2]['nodes'])
        old=values[2].get('native_authorities',[])
        self.assertEqual(catalog['native_authorities'][:len(old)],old)
        self.assertEqual(len(catalog['entries']),15)
        self.assertEqual(proof['catalog_canonical_sha256'],
            'cf25bf375f9905a8605eff5ebda5b217579657180b8c21f4521254fee7be2825')

    def test_missing_duplicate_unreviewed_or_mutated_receipts_fail_closed(self):
        faults=['missing','duplicate','run','digest','receipt','marker','entry','parent','request']
        for fault in faults:
            with self.subTest(fault=fault):
                values=self.values();publications=values[-1];approval=publications[0]['approval']
                if fault=='missing':publications.pop()
                elif fault=='duplicate':publications[1]=copy.deepcopy(publications[0])
                elif fault=='run':approval['controller_run']['id']+=1
                elif fault=='digest':approval['report_sha256']='0'*64
                elif fault=='receipt':publications[0]['report']['append']['new_bytes']+=1
                elif fault=='marker':publications[0]['report']['original_pool']['uid']='other'
                elif fault=='entry':approval['entry']['expires_at']='2099-01-01T00:00:00Z'
                elif fault=='parent':values[2]['entries'][0]['expires_at']='2099-01-01T00:00:00Z'
                elif fault=='request':values[4]['source_sha']='0'*40
                with self.assertRaises((ValueError,KeyError)):native.compose(*values)

    def test_materialize_then_actual_configure_and_source_record(self):
        fixture=FreshLifecycle()
        with tempfile.TemporaryDirectory(prefix='native-catalog-lifecycle-') as tmp:
            root,checkout,temp,head,_=fixture.fixture(tmp)
            source.provenance('check',checkout,temp,'fixture/matrixone',head,'matrixorigin/matrixone')
            catalog,proof=native.materialize(ROOT,temp)
            self.assertEqual(catalog.parent,temp)
            self.assertFalse((temp/'cs-node-cache').exists())
            self.assertEqual(proof['native_additions'],2)
            fixture.configure(root,checkout,temp,catalog)
            source.provenance('record',checkout,temp,'fixture/matrixone',head,'matrixorigin/matrixone')
            self.assertTrue((temp/'cs-node-cache/pr-source.json').is_file())
            with self.assertRaises(ValueError):native.materialize(ROOT,temp)
            with self.assertRaises(ValueError):fixture.configure(root,checkout,temp,catalog)


if __name__=='__main__':unittest.main()
