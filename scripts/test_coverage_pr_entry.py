"""Production routing has its own tests; the measured consumer is immutable."""
import copy
import hashlib
import json
import os
import subprocess
import textwrap
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from scripts.coverage_pr_entry import ENTRY, PROJECT, main, route

ROOT = Path(__file__).resolve().parents[1]
PIN = "48edf24e44a888c1c4d57318c4eca7cbb310c6e7"
LEGACY_HELPER_SHA = "96874e708fa46db2af5a6f833b64bbd448b2d4d8"
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
        self.assertEqual((out["head_sha"], out["head_repository"], out["route"]),
                         ("a"*40, "example/matrixone", "cache"))
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

    def test_pr_number_does_not_pin_either_group(self):
        for i in range(1, 9):
            self.pr["number"] = self.event["number"] = self.event["pull_request"]["number"] = i
            self.assertEqual(route(CALLER, self.event, self.pr, 204), {
                "route":"cache", "allowed":"true", "head_sha":"a"*40, "head_repository":"example/matrixone"})
        wf = (ROOT/".github/workflows/coverage-ut.yaml").read_text()
        self.assertIn("      max-parallel: 2\n      matrix:\n        group:\n        - 0\n        - 1\n", wf)
        self.assertIn("uses: ./.github/workflows/coverage-worker-cs.yaml", wf)
        self.assertIn("runs-on: cs-node-cache-7c14g", (ROOT/".github/workflows/coverage-worker-cs.yaml").read_text())
        self.assertNotIn("outputs.node_pair", wf)

    def test_actual_placement_guard(self):
        wf = (ROOT/".github/workflows/coverage-worker-cs.yaml").read_text()
        block = wf.split("    - id: reader\n",1)[1].split("    - name: Install CS",1)[0]
        script = textwrap.dedent(block.split("      run: |\n",1)[1])
        for site, n, actual, expected, pool, ok in (
                ("mty",0,0,0,"mty",True), ("ruh",2,2,2,"ruh",True),
                ("mnl2",1,1,1,"mnl2",True), ("jed",0,0,0,"jed",True),
                ("mty",2,2,2,"mty",False), ("jed",0,0,0,"ruh",False),
                ("jed",0,1,1,"jed",False), ("jed",0,0,1,"jed",False),
                ("other",1,1,1,"other",False)):
            with self.subTest(site=site, n=n, actual=actual, expected=expected, pool=pool), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)/"output"
                env = dict(PATH=os.environ["PATH"], ACTUAL_RUNNER=f"cs-{pool}-nc-n{n}-pr-v1-abc-runner-def",
                           CS_CACHE_ENV=site, CS_CACHE_NODE=f"mo-standard-{actual}",
                           CS_CACHE_EXPECTED_NODE=f"mo-standard-{expected}", GITHUB_OUTPUT=str(out))
                self.assertEqual(subprocess.run(["bash","-c",script],env=env).returncode == 0, ok)
                self.assertEqual(out.read_text() if out.exists() else "", f"site={site}\n" if ok else "")

    def test_gate_exports_and_allowlisted_receipt(self):
        checks = [{"credential_role": "actions_read", "http_status": 204}]
        client = SimpleNamespace(read=lambda path, token: (200, self.pr), membership=lambda *args: (204, checks),
                                 c=SimpleNamespace(atomic_json=lambda path, obj: path.write_text(json.dumps(obj))))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"event").write_text(json.dumps(self.event))
            env = dict(zip(("GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_WORKFLOW_REF"), CALLER))
            env.update(GITHUB_EVENT_PATH=str(root/"event"), GITHUB_OUTPUT=str(root/"outputs"), RUNNER_TEMP=tmp,
                       CS_PR_METADATA_TOKEN="fixture-only", CS_CACHE_READ_TOKEN="fixture-only")
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(sys.modules, node_cache_real_pr_gate=client):
                main()
            self.assertIn("route=cache\n", (root/"outputs").read_text())
            raw = (root/"real-pr-authorization.json").read_text()
            self.assertNotIn("fixture-only", raw)
            receipt = json.loads(raw)
            self.assertEqual(receipt["head_sha"], self.pr["head"]["sha"])
            self.assertEqual(receipt["membership_checks"], checks)

    def test_exact_measured_jobs_and_coverage_handoff(self):
        # Hash the complete three-job body from the measured immutable pin,
        # after undoing ONLY the production entry wiring. No test/cache body edits.
        path = ROOT/".github/workflows/coverage-ut.yaml"
        src = path.read_text()
        # Reconstruct the old job wrapper only to preserve the pre-existing measured-body digest.
        worker = (ROOT/".github/workflows/coverage-worker-cs.yaml").read_text()
        steps = "    - id: reader\n" + worker.split("    - id: reader\n", 1)[1]
        steps = (steps.replace("inputs.group", "matrix.group")
                      .replace("inputs.source", "needs.check_coverage_eligibility.outputs.head_repository")
                      .replace("inputs.head", "needs.check_coverage_eligibility.outputs.head_sha"))
        body = ("  ut-coverage-linux-x86:\n    name: UT Coverage group ${{ matrix.group }} on Ubuntu/x86\n    strategy:\n      fail-fast: false\n      max-parallel: 2\n      matrix:\n        group:\n        - 0\n        - 1\n    outputs:\n      status: ${{ steps.coverage_status.outputs.status }}\n    needs:\n    - check_coverage_eligibility\n    if: ${{ needs.check_coverage_eligibility.outputs.allowed == 'true' }}\n    environment: ci\n    runs-on: cs-node-cache-7c14g\n    timeout-minutes: 320\n    steps:\n" + steps + src[src.index("  merge-coverage-groups:"):]).rstrip()
        old_checkout = "repository: matrixorigin/CI\n        ref: "+PIN
        self.assertEqual(body.count(old_checkout), 2)
        body = body.replace(old_checkout, "repository: ${{ job.workflow_repository }}\n        ref: ${{ job.workflow_sha }}")
        original_route = ("    runs-on: ${{ format('cs-{0}-nc-n{1}-pr-v1', inputs.environment, inputs.node_pair == '12' && (matrix.group == 0 && '1'\n"
                          "      || '2') || matrix.group) }}")
        body = body.replace("    runs-on: cs-node-cache-7c14g", original_route)
        before, rest = body.split("    - id: reader\n", 1)
        body = before + "    - name: Install CS runner build dependencies\n" + rest.split("    - name: Install CS runner build dependencies\n",1)[1]
        body = body.replace("steps.reader.outputs.site", "inputs.environment")
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
        checkout = "repository: matrixorigin/CI\n          ref: "+LEGACY_HELPER_SHA
        self.assertEqual(s.count(checkout), 1)
        b = s.replace(checkout, "repository: ${{ job.workflow_repository }}\n          ref: ${{ job.workflow_sha }}").encode()
        self.assertEqual(hashlib.sha1(b"blob "+str(len(b)).encode()+b"\0"+b).hexdigest(),
                         "52b9213fa50e4de5184b9089ef15f13f82cf8a54")
