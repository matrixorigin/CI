"""Structural checks for the checked-in publication catalog, without network IO.

These checks do not replace protected publication or live producer validation.
"""
import copy
import datetime
import json
from pathlib import Path
import re
import unittest

import node_compile_cache as n


def check(catalog):
    assert catalog['schema'] == 2
    nodes = catalog['nodes']
    assert len({(b['environment'], b['node']) for b in nodes}) == len(nodes)
    assert len({b['pvc_uid'] for b in nodes}) == len(nodes)
    for binding in nodes:
        assert binding['environment'] in ('mty', 'mnl2', 'jed', 'ruh')
        assert re.fullmatch('[0-9a-f]{64}', binding['marker_sha256'])
        entries = n.catalog_for_node(catalog, binding)['entries']
        assert entries and len({e['index_sha256'] for e in entries}) == len(entries)
        for entry in entries:
            assert entry['runtime']['environment'] == binding['environment']
            assert entry['compile_only'] is True
            assert re.fullmatch('[0-9a-f]{40}', entry['source'])
            assert re.fullmatch('[0-9a-f]{64}', entry['index_sha256'])
            assert datetime.datetime.fromisoformat(entry['expires_at'].replace('Z', '+00:00')).tzinfo
        n.native_authorities(catalog, binding, entries)
    for entry in catalog['entries']:
        assert entry.get('bindings')
        for published in entry['bindings']:
            matches = [b for b in nodes if all(b[k] == v for k, v in published.items())]
            assert len(matches) == 1
            assert matches[0]['environment'] == entry['runtime']['environment']


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((Path(__file__).resolve().parents[1] /
                                  'config/node-compile-cache-catalog.json').read_text())

    def test_checked_in_catalog(self):
        check(self.catalog)

    def test_cross_environment_binding_rejected(self):
        self.catalog['entries'][0]['runtime']['environment'] = 'unknown'
        with self.assertRaises(AssertionError):
            check(self.catalog)

    def test_duplicate_or_unbound_generation_rejected(self):
        original = copy.deepcopy(self.catalog)
        self.catalog['entries'].append(copy.deepcopy(self.catalog['entries'][0]))
        with self.assertRaises(AssertionError):
            check(self.catalog)
        original['entries'][0]['bindings'] = []
        with self.assertRaises((AssertionError, ValueError)):
            check(original)


if __name__ == '__main__':
    unittest.main()
