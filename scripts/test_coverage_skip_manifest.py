import json
from pathlib import Path
import tempfile
import unittest

from scripts.coverage_skip_manifest import collect, compare, redact


class CoverageSkipManifestTest(unittest.TestCase):
    def manifest(self, events, sha="same", status="passed"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text("\n".join(json.dumps(e) for e in [{"Action": "start", "Package": "p"}] + events) + "\n")
            return collect(path, {"result": status}, sha, "runner", "test")

    def test_distinguishes_packages_and_tests_and_preserves_reasons(self):
        result = self.manifest([
            {"Action": "output", "Package": "p", "Test": "T", "Output": "a_test.go:10: missing TEST_CONFIG"},
            {"Action": "skip", "Package": "p", "Test": "T"},
            {"Action": "skip", "Package": "empty"},
        ])
        self.assertEqual(result["event_counts"]["skip"], 2)
        self.assertEqual(len(result["packages_without_tests"]), 1)
        self.assertEqual(result["skipped_tests"][0]["reasons"], ["a_test.go:10: missing TEST_CONFIG"])

    def test_original_skips_accepted_but_new_skips_rejected(self):
        old = self.manifest([{"Action": "skip", "Package": "p", "Test": "T"}])
        self.assertTrue(compare(old, old)["accepted"])
        new = self.manifest([{"Action": "skip", "Package": "p", "Test": "New"}])
        self.assertFalse(compare(old, new)["accepted"])

    def test_equal_counts_do_not_hide_different_tests(self):
        old = self.manifest([{"Action": "skip", "Package": "p", "Test": "A"}])
        new = self.manifest([{"Action": "skip", "Package": "p", "Test": "B"}])
        self.assertEqual(len(compare(old, new)["new_skips"]), 1)

    def test_different_source_or_failed_producer_not_accepted(self):
        old = self.manifest([])
        for new in (self.manifest([], sha="other"), self.manifest([], status="timeout")):
            self.assertFalse(compare(old, new)["accepted"])

    def test_missing_package_or_empty_execution_not_accepted(self):
        old = self.manifest([])
        new = self.manifest([])
        new["started_packages"] = []
        self.assertFalse(compare(old, new)["accepted"])
        self.assertFalse(compare(new, new)["accepted"])

    def test_failure_timeout_and_malformed_evidence_not_accepted(self):
        old = self.manifest([])
        new = self.manifest([
            {"Action": "output", "Package": "p", "Output": "panic: test timed out after 20m"},
            {"Action": "fail", "Package": "p"},
        ])
        self.assertFalse(compare(old, new)["accepted"])
        self.assertEqual(len(new["package_timeouts"]), 1)
        old["malformed_events"] = 1
        self.assertFalse(compare(old, old)["accepted"])

    def test_redacts_connections_and_credentials(self):
        value = redact("test.go:1: https://user:password@host/path key-secret=opaque dump:password@tcp(host:6001)/")
        self.assertNotIn("password", value)
        self.assertNotIn("opaque", value)
        self.assertNotIn("host", value)


if __name__ == "__main__":
    unittest.main()
