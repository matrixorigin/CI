import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import node_cache_cpu_producer_job as p


class ProducerJob(unittest.TestCase):
    def test_exact_cpu_only_difference_and_local_tools(self):
        cpu = {'vendor_id': 'GenuineIntel', 'cpu family': '6', 'model': '106', 'model name': 'Xeon',
               'stepping': '6', 'flags': 'fpu sse2 avx2'}
        runtime = {'environment': 'mty', 'repository': 'matrixorigin/matrixone', 'cpu': cpu, 'tools': 'unchanged'}
        state = {'config': {'runtime': runtime, 'shared': '/ci-cache/incremental-v2', 'indexes': []}, 'entries': []}
        binding = {'node': 'mo-standard-1'}
        tools = {'persistent_tools': True, 'external_tool_downloads': 0, 'binding': binding}
        parent = copy.deepcopy(runtime); parent['cpu']['model'] = '85'
        env = {'CS_CACHE_NODE': 'mo-standard-1', 'CS_CACHE_EXPECTED_NODE': 'mo-standard-1', 'CS_CACHE_ENV': 'mty',
               'CS_CACHE_NODE_UID': 'node', 'CS_CACHE_PVC_UID': 'pvc', 'CS_CACHE_PV_UID': 'pv'}
        for fault in (None, 'cpu', 'source', 'abi', 'tools', 'binding', 'existing'):
            local, t, old = copy.deepcopy((state, tools, parent)); revision = p.SOURCE
            if fault == 'cpu': local['config']['runtime']['cpu']['model'] = '85'
            if fault == 'source': revision = '0'*40
            if fault == 'abi': old['tools'] = 'changed'
            if fault == 'tools': t['external_tool_downloads'] = 1
            if fault == 'binding': t['binding'] = {'node': 'other'}
            if fault == 'existing': local['config']['indexes'] = ['f'*64]
            with patch.dict(os.environ, env), patch.dict(p.PROFILES, {'mo-standard-1': p.cpus.checked_cpu(cpu)}), \
                 patch.object(p.cache, 'verify_binding', return_value=binding), \
                 patch.object(p.cache, 'catalog_for_node', return_value={'entries': [{'runtime': old}]}):
                if fault:
                    with self.subTest(fault=fault), self.assertRaises(ValueError): p.preflight(local, {}, 'mo-standard-1', t, revision)
                else:
                    proof = p.preflight(local, {}, 'mo-standard-1', t, revision)
                    self.assertFalse(proof['ut_execution']); self.assertEqual(proof['shared_cache_writes'], 0)

    def test_compile_contract_requires_all_packages_and_own_native(self):
        contract = {'full_packages': ['p']*505, 'coverpkg': ['c']*484}
        result = {'compile_only': True, 'source': p.SOURCE, 'runtime': {'cpu': {}},
                  'native_manifests': {'key': 'manifest'}, 'compile_seconds': [1.]*17}
        with patch.object(p.candidate, 'scope', return_value=contract), \
             patch.object(p.c, 'read_json', return_value={'native_hit': False}), \
             patch.object(p.candidate, 'freeze', return_value=result) as freeze, \
             patch.object(p.cpus, 'checked_cpu', return_value=p.PROFILES['mo-standard-1']), \
             patch.dict(os.environ, {'CS_CACHE_NODE': 'mo-standard-1'}):
            self.assertEqual(p.freeze(Path('/private/state.json'), 'full', 'cover', 'destination'), result)
            freeze.assert_called_once_with(Path('/private/state.json'), contract, 'destination')
            contract['full_packages'].pop()
            with self.assertRaises(ValueError): p.freeze(Path('/private/state.json'), 'full', 'cover', 'destination')


if __name__ == '__main__': unittest.main()
