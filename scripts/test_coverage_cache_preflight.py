from io import BytesIO
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

from scripts.coverage_cache_preflight import preflight


REPO = "matrixorigin/matrixone"


def metadata_error(run, status=404, repo=REPO):
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run}"
    error = HTTPError(url, status, "fixture", None, BytesIO())
    error.close()
    return error


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.entries = [{"producer": {"repository": REPO, "id": n}, "runtime": {}}
                        for n in (101, 102)]
        self.grant = Mock()
        self.grant.covers.return_value = True
        self.grant.permits.return_value = True
        self.reviewed = SimpleNamespace(
            load=lambda root: ({"nodes": [0, 1]}, None),
            cache=SimpleNamespace(catalog_for_node=lambda catalog, node: {"entries": self.entries}),
            authority=Mock(return_value=self.grant),
            c=SimpleNamespace(require=self.require),
        )

    @staticmethod
    def require(ok, message):
        if not ok:
            raise ValueError(message)

    def test_missing_producer_does_not_prevent_checking_other_entries_or_nodes(self):
        self.grant.permits.side_effect = [metadata_error(101), True, metadata_error(101), True]
        self.assertEqual(preflight(None, self.reviewed, None), [101])
        self.assertEqual([args.args[0] for args in self.grant.permits.call_args_list], self.entries * 2)
        self.assertEqual(self.reviewed.authority.call_count, 2)

    def test_normal_catalog_has_no_missing_runs(self):
        self.assertEqual(preflight(None, self.reviewed, None), [])

    def test_only_the_exact_producer_lookup_can_miss(self):
        for error in (metadata_error(101, 403), metadata_error(101, 500), metadata_error(102),
                      metadata_error(101, repo="matrixorigin/CI")):
            with self.subTest(error=error):
                self.grant.permits.side_effect = error
                with self.assertRaises(HTTPError):
                    preflight(None, self.reviewed, None)

    def test_missing_authority_metadata_still_blocks(self):
        self.reviewed.authority.side_effect = metadata_error(101, repo="matrixorigin/CI")
        with self.assertRaises(HTTPError):
            preflight(None, self.reviewed, None)
        self.grant.permits.assert_not_called()

    def test_revocation_and_integrity_failures_still_block(self):
        self.grant.permits.return_value = False
        with self.assertRaisesRegex(ValueError, "revoked"):
            preflight(None, self.reviewed, None)
        self.grant.permits.side_effect = ValueError("producer provenance changed")
        with self.assertRaisesRegex(ValueError, "provenance"):
            preflight(None, self.reviewed, None)
        self.grant.covers.return_value = False
        with self.assertRaisesRegex(ValueError, "outside durable grant"):
            preflight(None, self.reviewed, None)


if __name__ == "__main__":
    unittest.main()
