import json
from pathlib import Path
import tempfile
import unittest

from coverage_two_groups_merge import validate


class MergeEvidenceTest(unittest.TestCase):
    def test_validation_requires_complete_healthy_disjoint_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for group in range(3):
                sub = root / str(group)
                sub.mkdir()
                plan = {'selected_group': group, 'scope_sha256': 'same', 'scope_count': 3,
                        'groups': [['pkg/a'], ['pkg/b'], ['pkg/c']]}
                report = {'producer_status': 'passed', 'malformed_events': 0, 'failed': [],
                          'package_timeouts': [], 'started_packages': [plan['groups'][group][0]],
                          'source_sha': 'same'}
                (sub / 'coverage-group-plan.json').write_text(json.dumps(plan))
                (sub / 'ut-coverage-skip-manifest.json').write_text(json.dumps(report))
            validate(root)
            report['started_packages'] = []
            (root / '1' / 'ut-coverage-skip-manifest.json').write_text(json.dumps(report))
            with self.assertRaises(ValueError):
                validate(root)


if __name__ == '__main__':
    unittest.main()
