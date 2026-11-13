import json
import tempfile
import unittest
from pathlib import Path

from scripts.coverage_timing import seconds, summarize


class TimingTests(unittest.TestCase):
    def test_duration(self):
        self.assertAlmostEqual(seconds('1m2.5s'), 62.5)
        self.assertAlmostEqual(seconds('42µs'), 0.000042)
        with self.assertRaises(ValueError):
            seconds('1s junk')

    def test_observed_only_and_package_terminal(self):
        events = [
            {'Package': 'p', 'Action': 'output', 'Test': 'T', 'Output': 'MO_UT_SETUP phase=admission-acquire duration=2.5s status=ready'},
            {'Package': 'p', 'Action': 'pass', 'Test': 'T', 'Elapsed': 3},
            {'Package': 'p', 'Action': 'pass', 'Elapsed': 10},
            {'Package': 'q', 'Action': 'start'},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report'
            report.write_text(''.join(json.dumps(e) + '\n' for e in events))
            result = summarize(report)
        self.assertEqual(result['packages'], {'p': {'action': 'pass', 'elapsed_seconds': 10}})
        self.assertEqual(result['admission_observations']['p'][0]['seconds'], 2.5)
        self.assertEqual(result['parse_errors'], 0)


if __name__ == '__main__':
    unittest.main()
