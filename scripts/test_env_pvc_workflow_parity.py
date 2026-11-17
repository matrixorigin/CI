from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]


class EnvironmentWorkflowParity(unittest.TestCase):
    def test_only_audited_environment_bindings_differ(self):
        base = (ROOT/'.github/workflows/coverage-mty-pvc-consumer.yaml').read_text()
        for env, baseline, producer, caller, suffix in (
            ('jed', '44033509326', '44040765865', '6b0121525da561211b76e647c10f374304d79c25', 'cpe8k'),
            ('ruh', '44035926394', '44041833471', 'd1521fc1b05d41b5c501a92ee202ab1659c2dd7d', 'rhprep'),
        ):
            with self.subTest(environment=env):
                expected = base
                replacements = (
                    ('Isolated MTY', 'Isolated '+env.upper()),
                    ('CONFIG: ${{ secrets.CS_S3_MTY_CONFIG }}', 'CONFIG: ${{ secrets.CS_S3_'+env.upper()+'_CONFIG }}'),
                    ('cs-mty-pvc-g{0}-cpe8k', 'cs-'+env+'-pvc-g{0}-'+suffix),
                    ('CS_CACHE_ENV: mty', 'CS_CACHE_ENV: '+env),
                    ('            mty) ;;', '            '+env+') ;;'),
                    ("entry['environment']=='mty'", "entry['environment']=='"+env+"'"),
                    ("entry['fingerprint']['cs_environment']=='mty'", "entry['fingerprint']['cs_environment']=='"+env+"'"),
                    ('44032472330', baseline), ('44036614881', producer),
                    ('6b0121525da561211b76e647c10f374304d79c25', caller),
                )
                for old, new in replacements:
                    self.assertIn(old, expected)
                    expected = expected.replace(old, new)
                actual = (ROOT/f'.github/workflows/coverage-{env}-pvc-consumer.yaml').read_text()
                self.assertEqual(actual, expected)
                self.assertIn('test "$CS_S3_ENV" = "$CS_CACHE_ENV"', actual)
                self.assertIn('test "$HIT" = true', actual)
                self.assertIn('verify-native', actual)
                self.assertIn('verify-scope', actual)
                self.assertIn("if k not in ('run','attempt')", actual)


if __name__ == '__main__':
    unittest.main()
