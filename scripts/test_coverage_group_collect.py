import tempfile
import unittest
from pathlib import Path

from coverage_group_collect import collect


class CollectTest(unittest.TestCase):
    def test_latest_matching_attempts_per_group(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            groups, profiles = root / 'groups', root / 'profiles'
            groups.mkdir()
            profiles.mkdir()
            for group, attempt in [(0, 1), (1, 2), (2, 1)]:
                evidence = groups / f'ut-coverage-skips-group-{group}-123-{attempt}'
                profile = profiles / f'ut-coverage-group-{group}-generation-123-{attempt}'
                evidence.mkdir()
                profile.mkdir()
                for name in ('coverage-group-plan.json', 'ut-coverage-skip-manifest.json'):
                    (evidence / name).write_text('{}')
                (profile / 'ut-coverage.out').write_text('mode: set\n')
            collect(groups, profiles, root / 'selected', '123')
            self.assertTrue((root / 'selected/groups/1/coverage-group-plan.json').is_file())
            self.assertTrue((root / 'selected/profiles/2/ut-coverage.out').is_file())

    def test_refuses_mismatched_or_missing_latest_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            groups, profiles = root / 'groups', root / 'profiles'
            groups.mkdir()
            profiles.mkdir()
            (groups / 'ut-coverage-skips-group-0-123-2').mkdir()
            (profiles / 'ut-coverage-group-0-generation-123-1').mkdir()
            with self.assertRaises(ValueError):
                collect(groups, profiles, root / 'selected', '123')


if __name__ == '__main__':
    unittest.main()
