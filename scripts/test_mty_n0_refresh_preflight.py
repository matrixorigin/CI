import copy
import unittest
from unittest.mock import patch

import mty_n0_refresh_preflight as probe
import mty_n0_prepare_group1 as prepare


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.assertEqual(probe.c.digest(probe.AUTH), probe.AUTH_SHA)
        catalog = probe.c.read_json(probe.AUTH)['base_catalog']
        binding, = [n for n in catalog['nodes'] if n['node'] == probe.NODE]
        self.parents = [e['runtime'] for e in probe.n.catalog_for_node(catalog, binding)['entries']]
        self.current = copy.deepcopy(self.parents[0])
        self.current['system_packages_sha256'] = 'f'*64

    def test_package_only(self):
        self.assertIn(self.parents[0]['system_packages_sha256'],
                      probe.check_runtime(self.current, self.parents))

    def test_existing(self):
        with self.assertRaises(ValueError):
            probe.check_runtime(self.current, [*self.parents, self.current])

    def test_reject_other_drift(self):
        for field in ('cpu', 'tool_binaries', 'workspace', 'go_env'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                probe.check_runtime({**self.current,field:'different'}, self.parents)

    def test_no_parent(self):
        with self.assertRaises(ValueError):
            probe.check_runtime(self.current, [])

    def test_wrong_node(self):
        with patch.dict('os.environ', {'CS_CACHE_ENV':'mty','CS_CACHE_NODE':'mo-standard-1',
                                      'CS_CACHE_EXPECTED_NODE':'mo-standard-1'}, clear=True):
            with self.assertRaisesRegex(ValueError,'node0'):
                probe.inspect()

    def test_group1_only(self):
        selected = [f'p{i}' for i in range(250)]
        plan = {'scope_sha256':prepare.COLD_SCOPE, 'groups':[['other'], selected]}
        contract = {'full_packages':selected+['other'], 'packages':selected,
                    'coverpkg':list(range(476))}
        with patch.object(prepare.groups, 'plan', return_value=plan):
            prepare.check_scope(contract)
            for key, value in [('packages',['other']), ('coverpkg',[])]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    prepare.check_scope({**contract,key:value})
            plan['scope_sha256'] = '0'*64
            with self.assertRaises(ValueError):
                prepare.check_scope(contract)
