import unittest
from scripts.coverage_two_groups import EMBED, FILE_SERVICE, ROOT, plan


class CoverageTwoGroupsTest(unittest.TestCase):
    def test_deterministic_complete_and_disjoint(self):
        packages = [FILE_SERVICE, ROOT+'pkg/embed', ROOT+'pkg/tests/sqlintegration', ROOT+'pkg/unknown', ROOT+'pkg/a']
        a, b = plan(packages), plan(list(reversed(packages)))
        self.assertEqual(a, b)
        self.assertEqual(set().union(*map(set, a['groups'])), set(packages))
        self.assertEqual(sum(map(len, a['groups'])), len(packages))
        self.assertIn(FILE_SERVICE, a['groups'][0])
        self.assertIn(EMBED, a['groups'][2])

    def test_rejects_missing_and_duplicate(self):
        with self.assertRaises(ValueError):
            plan([ROOT+'pkg/embed'])
        with self.assertRaises(ValueError):
            plan([FILE_SERVICE, FILE_SERVICE])

    def test_service_partition_preserves_complete_scope(self):
        packages = [FILE_SERVICE, EMBED, ROOT+'pkg/tests/service', ROOT+'pkg/tests/sqlintegration', ROOT+'pkg/tests/issues/isolated', ROOT+'pkg/a']
        result = plan(packages)
        self.assertEqual(set().union(*map(set, result['groups'])), set(packages))
        self.assertEqual(sum(map(len, result['groups'])), len(packages))
        self.assertEqual(sum(ROOT+'pkg/tests/service' in g for g in result['groups']), 1)


if __name__ == '__main__':
    unittest.main()
