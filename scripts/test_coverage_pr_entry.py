"""Production routing has its own tests; the measured consumer is immutable."""
import copy
import hashlib
from pathlib import Path
import unittest
from scripts.coverage_pr_entry import ENTRY, PROJECT, route

ROOT = Path(__file__).resolve().parents[1]
PIN = "48edf24e44a888c1c4d57318c4eca7cbb310c6e7"
OLD = "5fc75d89bc2841e50ae1557d28ebd9aaec6ec3e2"
CALLER = (PROJECT, "pull_request_target", ENTRY)


class EntryTest(unittest.TestCase):
    def setUp(self):
        self.pr = {"number": 32871, "state": "open", "draft": False, "labels": [{"name": "kind/test-ci"}],
                   "base": {"repo": {"full_name": PROJECT}, "ref": "main"},
                   "head": {"repo": {"full_name": "example/matrixone", "archived": False}, "sha": "a"*40},
                   "user": {"login": "example"}}
        self.event = {"number": 32871, "action": "synchronize", "pull_request": copy.deepcopy(self.pr)}

    def test_fork_and_source_are_bound_to_the_event(self):
        out = route(CALLER, self.event, self.pr, 204)
        self.assertEqual((out["head_sha"], out["head_repository"], out["route"], out["node_pair"]),
                         ("a"*40, "example/matrixone", "cache", "01"))
        for target, key, value in [(self.pr["head"], "sha", "b"*40),
                                   (self.pr["head"]["repo"], "full_name", "other/matrixone"),
                                   (self.pr, "state", "closed"), (self.pr, "number", 1)]:
            old = target[key]; target[key] = value
            with self.assertRaises(ValueError):
                route(CALLER, self.event, self.pr, 204)
            target[key] = old

    def test_unverified_requests_cannot_get_cache_runners(self):
        for status in (403, 401, 500):
            with self.assertRaises(ValueError):
                route(CALLER, self.event, self.pr, status)
        self.assertEqual(route(CALLER, self.event, self.pr, 404)["route"], "legacy")
        self.assertEqual(route(CALLER, self.event, self.pr, 302)["route"], "legacy")
        self.pr["labels"] = [{"name": "safe-to-test"}]
        self.assertEqual(route(CALLER, self.event, self.pr, 204)["route"], "legacy")
        for ctx in [(PROJECT, "workflow_dispatch", ENTRY), (PROJECT, "pull_request", ENTRY),
                    ("example/matrixone", CALLER[1], ENTRY), (*CALLER[:2], ENTRY+"x")]:
            with self.assertRaises(ValueError):
                route(ctx, self.event, self.pr, 204)

    def test_registered_pairs_only(self):
        routes = set()
        for i in range(1, 9):
            self.pr["number"] = self.event["number"] = self.event["pull_request"]["number"] = i
            out = route(CALLER, self.event, self.pr, 204, "mnl2,jed,ruh")
            routes.add((out["environment"], out["node_pair"]))
        self.assertEqual(routes, {(e,p) for e in ("mty","mnl2","jed","ruh") for p in ("01","12")}
                         - {("mty","12")})
        with self.assertRaises(ValueError):
            route(CALLER, self.event, self.pr, 204, "unreviewed")

    def test_exact_measured_jobs_and_coverage_handoff(self):
        # Hash the complete three-job body from the measured immutable pin,
        # after undoing ONLY the production entry wiring. No test/cache body edits.
        path = ROOT/".github/workflows/coverage-ut.yaml"
        src = path.read_text()
        body = src[src.index("  ut-coverage-linux-x86:"):].rstrip()
        old_checkout = "repository: matrixorigin/CI\n        ref: "+PIN
        self.assertEqual(body.count(old_checkout), 2)
        body = body.replace(old_checkout, "repository: ${{ job.workflow_repository }}\n        ref: ${{ job.workflow_sha }}")
        body = body.replace("needs.check_coverage_eligibility.outputs.environment", "inputs.environment")
        body = body.replace("needs.check_coverage_eligibility.outputs.node_pair", "inputs.node_pair")
        generation = "${{ inputs.bvt_generation || format"
        self.assertEqual(body.count(generation), 1)
        body = body.replace(generation, "${{ '' || format")
        # Repo-pinned actionlint rejects an unconditional 'if: true'. Its
        # absence has the exact same default success() behavior.
        label = "      name: Configure reviewed node cache with private incremental writes\n"
        self.assertEqual(body.count(label), 1)
        body = body.replace(label, label+"      if: ${{ true }}\n")
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), "8cb2793dcfa0eb8ba74bb78828895bd1fe36e79a6c73bfba5519aa80d59671de")
        for field in ("coverage_ready:", "coverage_status:", "bvt_generation:", "cache_seed:"):
            self.assertIn(field, src.split("jobs:\n")[0])

    def test_fallback_matches_existing_default_blob(self):
        s = (ROOT/".github/workflows/coverage-ut-pre-cutover.yaml").read_text()
        checkout = "repository: matrixorigin/CI\n          ref: "+OLD
        self.assertEqual(s.count(checkout), 1)
        b = s.replace(checkout, "repository: ${{ job.workflow_repository }}\n          ref: ${{ job.workflow_sha }}").encode()
        self.assertEqual(hashlib.sha1(b"blob "+str(len(b)).encode()+b"\0"+b).hexdigest(),
                         "12c2e0d3eccbb2b0f7e21b3379463cef9fa17498")
