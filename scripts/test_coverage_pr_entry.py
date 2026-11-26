"""Production routing has its own tests; the measured consumer is immutable."""
import copy
import hashlib
from http.client import IncompleteRead, RemoteDisconnected
import json
import os
import subprocess
import textwrap
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from unittest import mock
from scripts.coverage_pr_entry import (
    ENTRY, PROJECT, RELEASE_BASE, RELEASE_ENTRY, RELEASE_S3_FIXTURE_BLOBS,
    S3_FIXTURE_BLOBS, has_s3_fixture, main, route,
)

ROOT = Path(__file__).resolve().parents[1]
PIN = "48edf24e44a888c1c4d57318c4eca7cbb310c6e7"
LEGACY_HELPER_SHA = "96874e708fa46db2af5a6f833b64bbd448b2d4d8"
CALLER = (PROJECT, "pull_request_target", ENTRY)
RELEASE_CALLER = (PROJECT, "pull_request_target", RELEASE_ENTRY)


class EntryTest(unittest.TestCase):
    def setUp(self):
        self.verify_fixture = mock.Mock(return_value=True)
        self.pr = {"number": 32871, "state": "open", "draft": False, "labels": [{"name": "size/XL"}],
                   "base": {"repo": {"full_name": PROJECT}, "ref": "main"},
                   "head": {"repo": {"full_name": "example/matrixone", "archived": False}, "sha": "a"*40},
                   "user": {"login": "example"}}
        self.event = {"number": 32871, "action": "synchronize", "pull_request": copy.deepcopy(self.pr)}

    def test_fork_and_source_are_bound_to_the_event(self):
        out = route(CALLER, self.event, self.pr, 204, self.verify_fixture)
        self.assertEqual((out["head_sha"], out["head_repository"], out["route"]),
                         ("a"*40, "example/matrixone", "cache"))
        for target, key, value in [(self.pr["head"], "sha", "b"*40),
                                   (self.pr["head"]["repo"], "full_name", "other/matrixone"),
                                   (self.pr, "state", "closed"), (self.pr, "number", 1)]:
            old = target[key]; target[key] = value
            with self.assertRaises(ValueError):
                route(CALLER, self.event, self.pr, 204, self.verify_fixture)
            target[key] = old

    def test_unverified_requests_cannot_get_cache_runners(self):
        for status in (403, 401, 500):
            with self.assertRaises(ValueError):
                route(CALLER, self.event, self.pr, status, self.verify_fixture)
        self.assertEqual(route(CALLER, self.event, self.pr, 404, self.verify_fixture)["route"], "legacy")
        self.assertEqual(route(CALLER, self.event, self.pr, 302, self.verify_fixture)["route"], "legacy")
        for label in ("safe-to-test", "kind/test-ci"):
            self.pr["labels"] = [{"name": label}]
            self.assertEqual(route(CALLER, self.event, self.pr, 404, self.verify_fixture)["route"], "legacy")
        for ctx in [(PROJECT, "workflow_dispatch", ENTRY), (PROJECT, "pull_request", ENTRY),
                    ("example/matrixone", CALLER[1], ENTRY), (*CALLER[:2], ENTRY+"x")]:
            with self.assertRaises(ValueError):
                route(ctx, self.event, self.pr, 204, self.verify_fixture)

    def test_only_main_is_enabled_without_rollout_opt_in(self):
        for base, expected in (("main", "cache"), ("4.2-dev", "legacy"),
                               ("4.1-dev", "legacy"), ("4.2-dev-other", "legacy"),
                               ("v4.2.8-hotfix", "legacy"), ("3.0-dev", "legacy")):
            with self.subTest(base=base):
                self.event["pull_request"]["base"]["ref"] = self.pr["base"]["ref"] = base
                self.assertEqual(route(CALLER, self.event, self.pr, 204, self.verify_fixture)["route"], expected)
        self.event["pull_request"]["base"]["ref"] = self.pr["base"]["ref"] = "4.2-dev"
        self.assertEqual(route(CALLER, self.event, self.pr, 404, self.verify_fixture)["route"], "legacy")
        self.event["pull_request"]["base"]["ref"] = self.pr["base"]["ref"] = "main"
        for labels in ([], [{"name": "kind/refactor"}]):
            self.pr["labels"] = labels
            self.assertEqual(route(CALLER, self.event, self.pr, 204, self.verify_fixture)["route"], "cache")
        self.pr["head"]["repo"]["archived"] = True
        self.assertEqual(route(CALLER, self.event, self.pr, 204, self.verify_fixture)["route"], "legacy")
        self.pr["head"]["repo"]["archived"] = False
        self.pr["base"]["ref"] = "4.2-dev"
        with self.assertRaisesRegex(ValueError, "stale or invalid PR identity"):
            route(CALLER, self.event, self.pr, 204, self.verify_fixture)

    def test_release_requires_opt_in_membership_and_exact_fixture(self):
        self.event["pull_request"]["base"]["ref"] = self.pr["base"]["ref"] = RELEASE_BASE
        self.pr["draft"] = self.event["pull_request"]["draft"] = True
        self.pr["labels"] = self.event["pull_request"]["labels"] = []
        for caller in (RELEASE_CALLER, CALLER):
            for enabled, member, valid, expected in (
                    (False, 204, True, "legacy"), (True, 204, True, "cache"),
                    (True, 204, False, "legacy"), (True, 302, True, "legacy"),
                    (True, 404, True, "legacy")):
                with self.subTest(caller=caller, enabled=enabled, member=member, valid=valid):
                    fixture = mock.Mock(return_value=valid)
                    result = route(caller, self.event, self.pr, member, fixture, enable_4_2_dev=enabled)
                    self.assertEqual(result["route"], expected)
                    if enabled and member == 204:
                        fixture.assert_called_once_with("example/matrixone", "a"*40)
                    else:
                        fixture.assert_not_called()
        self.pr["head"]["sha"] = "b"*40
        with self.assertRaisesRegex(ValueError, "stale or invalid PR identity"):
            route(RELEASE_CALLER, self.event, self.pr, 204, self.verify_fixture, enable_4_2_dev=True)
        self.pr["head"]["sha"] = "a"*40
        self.event["pull_request"]["base"]["ref"] = self.pr["base"]["ref"] = "4.1-dev"
        with self.assertRaisesRegex(ValueError, "unexpected base"):
            route(RELEASE_CALLER, self.event, self.pr, 204, self.verify_fixture, enable_4_2_dev=True)
        self.assertEqual(route(CALLER, self.event, self.pr, 204, self.verify_fixture,
                               enable_4_2_dev=True)["route"], "legacy")

    def test_release_fixture_blobs_are_not_approved_for_main(self):
        # Snapshot from the pinned MatrixOne tree, independently checked by CI.
        snapshot = json.loads((ROOT / "scripts/testdata/coverage_4_2_fixture.json").read_text())
        release = {name: {sha} for name, sha in snapshot["files"].items()}
        self.assertEqual(RELEASE_S3_FIXTURE_BLOBS, release)
        cases = [(release, RELEASE_BASE, True), (release, "main", False),
                 (S3_FIXTURE_BLOBS, RELEASE_BASE, False)]
        for name in release:
            missing = release.copy()
            del missing[name]
            cases.append((missing, RELEASE_BASE, False))
            cases.append(({**release, name: {"b"*40}}, RELEASE_BASE, False))
        for blobs, base, expected in cases:
            with self.subTest(base=base, expected=expected), \
                 mock.patch("scripts.coverage_pr_entry.build_opener") as opener:
                files = [{"type": "file", "name": name, "sha": sorted(shas)[0]} for name, shas in blobs.items()]
                response = opener.return_value.open.return_value.__enter__.return_value
                response.getheader.return_value = None
                response.read.return_value = json.dumps(files).encode()
                self.assertEqual(has_s3_fixture(PROJECT, snapshot["commit"], "fixture-only", base=base), expected)
                request = opener.return_value.open.call_args.args[0]
                self.assertTrue(request.full_url.endswith("?ref=" + snapshot["commit"]))

    def test_drafts_use_the_same_routing_and_identity_checks(self):
        self.pr["draft"] = self.event["pull_request"]["draft"] = True
        for base, member, expected in (("main", 204, "cache"), ("4.2-dev", 204, "legacy"),
                                       ("main", 404, "legacy"), ("4.2-dev", 302, "legacy"),
                                       ("4.1-dev", 204, "legacy")):
            for action in ("opened", "synchronize"):
                for labels in ([], [{"name": "kind/test-ci"}]):
                    with self.subTest(base=base, member=member, action=action, labels=labels):
                        self.event["action"] = action
                        self.event["pull_request"]["base"]["ref"] = self.pr["base"]["ref"] = base
                        self.pr["labels"] = labels
                        self.assertEqual(route(CALLER, self.event, self.pr, member, self.verify_fixture)["route"], expected)
        self.pr["base"]["ref"] = self.event["pull_request"]["base"]["ref"] = "main"
        self.pr["head"]["repo"]["archived"] = True
        self.assertEqual(route(CALLER, self.event, self.pr, 204, self.verify_fixture)["route"], "legacy")
        self.pr["head"]["repo"]["archived"] = False
        self.pr["head"]["sha"] = "b"*40
        with self.assertRaisesRegex(ValueError, "stale or invalid PR identity"):
            route(CALLER, self.event, self.pr, 204, self.verify_fixture)

    def test_unlabelled_main_head_requires_reviewed_s3_fixture(self):
        self.pr["labels"] = self.event["pull_request"]["labels"] = []
        files = [{"type": "file", "name": name, "sha": sorted(shas)[0]}
                 for name, shas in S3_FIXTURE_BLOBS.items()]
        verify = lambda source, sha: has_s3_fixture(source, sha, "fixture-only")
        for listing, expected in ((files[1:], "legacy"), (files, "cache"),
                                  ([dict(files[0], sha="b"*40), *files[1:]], "legacy")):
            with self.subTest(expected=expected), mock.patch("scripts.coverage_pr_entry.build_opener") as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.getheader.return_value = None
                response.read.return_value = json.dumps(listing).encode()
                self.assertEqual(route(CALLER, self.event, self.pr, 204, verify)["route"], expected)
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url,
                                 "https://api.github.com/repos/example/matrixone/contents/pkg/fileservice?ref=" + "a"*40)
        with mock.patch("scripts.coverage_pr_entry.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://api.github.com", 404, "missing", {}, None)
            self.assertEqual(route(CALLER, self.event, self.pr, 204, verify)["route"], "legacy")

    def test_fixture_transport_errors_keep_legacy_route(self):
        verify = lambda source, sha: has_s3_fixture(source, sha, "fixture-only")
        for error in (RemoteDisconnected("peer closed the connection"), ConnectionResetError(),
                      IncompleteRead(b"[", 10)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch("scripts.coverage_pr_entry.build_opener") as opener:
                open_request = opener.return_value.open
                if isinstance(error, IncompleteRead):
                    response = open_request.return_value.__enter__.return_value
                    response.getheader.return_value = None
                    response.read.side_effect = error
                else:
                    open_request.side_effect = error
                self.assertEqual(route(CALLER, self.event, self.pr, 204, verify),
                                 {"route": "legacy", "allowed": "false"})

    def test_fixture_response_shorter_than_content_length_keeps_legacy_route(self):
        files = [{"type": "file", "name": name, "sha": sorted(shas)[0]}
                 for name, shas in S3_FIXTURE_BLOBS.items()]
        raw = json.dumps(files).encode()
        with mock.patch("scripts.coverage_pr_entry.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = raw
            response.getheader.return_value = str(len(raw) + 10)
            verify = lambda source, sha: has_s3_fixture(source, sha, "fixture-only")
            self.assertEqual(route(CALLER, self.event, self.pr, 204, verify),
                             {"route": "legacy", "allowed": "false"})

    def test_pr_number_does_not_pin_either_group(self):
        for i in range(1, 9):
            self.pr["number"] = self.event["number"] = self.event["pull_request"]["number"] = i
            self.assertEqual(route(CALLER, self.event, self.pr, 204, self.verify_fixture), {
                "route":"cache", "allowed":"true", "head_sha":"a"*40, "head_repository":"example/matrixone"})
        wf = (ROOT/".github/workflows/coverage-ut.yaml").read_text()
        self.assertIn("      max-parallel: 2\n      matrix:\n        group:\n        - 0\n        - 1\n", wf)
        self.assertIn("uses: ./.github/workflows/coverage-worker-cs.yaml", wf)
        worker = (ROOT/".github/workflows/coverage-worker-cs.yaml").read_text()
        self.assertIn("""'["cs-node-cache-7c14g"]'""", worker)
        self.assertIn("cs-{0}-node-cache-all", worker)
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
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(sys.modules, node_cache_real_pr_gate=client), \
                 mock.patch("scripts.coverage_pr_entry.has_s3_fixture", self.verify_fixture):
                main()
            self.verify_fixture.assert_called_once_with("example/matrixone", "a"*40, "fixture-only")
            self.assertIn("route=cache\n", (root/"outputs").read_text())
            raw = (root/"real-pr-authorization.json").read_text()
            self.assertNotIn("fixture-only", raw)
            receipt = json.loads(raw)
            self.assertEqual(receipt["schema"], 2)
            self.assertEqual(receipt["head_sha"], self.pr["head"]["sha"])
            self.assertEqual(receipt["base_ref"], self.pr["base"]["ref"])
            self.assertEqual(receipt["membership_checks"], checks)

    def test_group_retry_uses_verified_head_and_rechecks_live_authorization(self):
        workflows = ROOT / ".github/workflows"
        parent = (workflows / "coverage-ut-standby.yaml").read_text()
        handoff = (workflows / "coverage-queue-handoff.yaml").read_text()
        self.assertIn("source: ${{ needs.check_coverage_eligibility.outputs.head_repository }}", parent)
        self.assertIn("head: ${{ needs.check_coverage_eligibility.outputs.head_sha }}", parent)
        self.assertIn("COVERAGE_ADMITTED_SOURCE: ${{ inputs.source }}", handoff)
        self.assertIn("COVERAGE_ADMITTED_HEAD: ${{ inputs.head }}", handoff)
        gate = handoff.split("name: Verify retry eligibility and pinned source", 1)[1]
        gate = gate.split("      run: ", 1)[1].splitlines()[0]
        source = self.pr["head"]["repo"]["full_name"]
        head = self.pr["head"]["sha"]
        for label, attempt, admitted_source, admitted_head, member, stale, allowed in (
                ("same verified head", "2", source, head, 204, False, True),
                ("different verified head", "2", source, "b"*40, 204, False, False),
                ("different verified source", "2", "another/matrixone", head, 204, False, False),
                ("missing verified source", "2", "", head, 204, False, False),
                ("unverified membership", "2", source, head, 404, False, False),
                ("first attempt", "1", source, head, 204, False, False),
                ("stale event", "2", source, head, 204, True, False)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "event").write_text(json.dumps(self.event))
                live = copy.deepcopy(self.pr)
                if stale:
                    live["head"]["sha"] = "c"*40
                client = SimpleNamespace(
                    read=mock.Mock(return_value=(200, live)),
                    membership=mock.Mock(return_value=(member, [])),
                    c=SimpleNamespace(atomic_json=lambda path, obj: path.write_text(json.dumps(obj))),
                )
                env = dict(zip(("GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_WORKFLOW_REF"), CALLER))
                env.update(GITHUB_EVENT_PATH=str(root / "event"), GITHUB_OUTPUT=str(root / "outputs"),
                           RUNNER_TEMP=tmp, GITHUB_RUN_ATTEMPT=attempt, CS_PR_METADATA_TOKEN="fixture-only",
                           CS_CACHE_READ_TOKEN="fixture-only", COVERAGE_ADMITTED_SOURCE=admitted_source,
                           COVERAGE_ADMITTED_HEAD=admitted_head)
                with mock.patch.dict(os.environ, env, clear=True), \
                     mock.patch.dict(sys.modules, node_cache_real_pr_gate=client), \
                     mock.patch("scripts.coverage_pr_entry.has_s3_fixture", return_value=False) as lookup:
                    if stale:
                        with self.assertRaisesRegex(ValueError, "stale or invalid PR identity"):
                            main()
                    else:
                        main()
                    lookup.assert_not_called()
                client.read.assert_called_once()
                client.membership.assert_called_once()
                self.assertEqual((root / "real-pr-authorization.json").exists(), allowed)
                if stale:
                    self.assertFalse((root / "outputs").exists())
                    continue
                outputs = dict(line.split("=", 1) for line in (root / "outputs").read_text().splitlines())
                gate_env = dict(os.environ, ALLOWED=outputs["allowed"], LIVE_HEAD=outputs.get("head_sha", ""),
                                LIVE_SOURCE=outputs.get("head_repository", ""), HEAD=admitted_head,
                                SOURCE=admitted_source)
                self.assertEqual(subprocess.run(["bash", "-c", gate], env=gate_env).returncode,
                                 0 if allowed else 1)

    def test_release_rollout_opt_in_is_not_available_in_the_legacy_workflow(self):
        workflows = ROOT / ".github/workflows"
        standby = (workflows / "coverage-ut-standby.yaml").read_text()
        handoff = (workflows / "coverage-queue-handoff.yaml").read_text()
        self.assertNotIn("COVERAGE_ENABLE_4_2_DEV", (workflows / "coverage-ut.yaml").read_text())
        self.assertIn("COVERAGE_ENABLE_4_2_DEV: ${{ inputs.enable_4_2_dev }}", standby)
        self.assertIn("enable_4_2_dev: ${{ inputs.enable_4_2_dev }}", standby)
        self.assertIn("COVERAGE_ENABLE_4_2_DEV: ${{ inputs.enable_4_2_dev }}", handoff)
        self.assertIn(RELEASE_ENTRY, handoff)
        self.assertNotIn("enable_cs_site_retry", (workflows / "coverage-ut.yaml").read_text())
        self.assertIn("enable_cs_site_retry: ${{ inputs.enable_cs_site_retry }}", standby)
        self.assertIn("COVERAGE_CS_SITE_RETRY: ${{ inputs.enable_cs_site_retry }}", handoff)

    def test_release_retry_checks_fixture_again_and_emits_exact_base(self):
        self.event["pull_request"]["base"]["ref"] = self.pr["base"]["ref"] = RELEASE_BASE
        client = SimpleNamespace(
            read=lambda *_: (200, self.pr), membership=lambda *_: (204, []),
            c=SimpleNamespace(atomic_json=lambda path, obj: path.write_text(json.dumps(obj))),
        )
        for enabled, verified, admitted_head, expected in (
                ("true", True, "a"*40, "cache"), ("true", False, "a"*40, "legacy"),
                ("true", True, "b"*40, "legacy"), ("false", True, "a"*40, "legacy")):
            with self.subTest(enabled=enabled, verified=verified, admitted_head=admitted_head), \
                 tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "event").write_text(json.dumps(self.event))
                env = dict(zip(("GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_WORKFLOW_REF"), RELEASE_CALLER))
                env.update(GITHUB_EVENT_PATH=str(root / "event"), GITHUB_OUTPUT=str(root / "outputs"),
                           RUNNER_TEMP=tmp, GITHUB_RUN_ATTEMPT="2", CS_PR_METADATA_TOKEN="fixture-only",
                           CS_CACHE_READ_TOKEN="fixture-only", COVERAGE_ENABLE_4_2_DEV=enabled,
                           COVERAGE_CS_SITE_RETRY="true",
                           COVERAGE_ADMITTED_SOURCE="example/matrixone", COVERAGE_ADMITTED_HEAD=admitted_head)
                with mock.patch.dict(os.environ, env, clear=True), \
                     mock.patch.dict(sys.modules, node_cache_real_pr_gate=client), \
                     mock.patch("scripts.coverage_pr_entry.has_s3_fixture", return_value=verified) as fixture:
                    main()
                if enabled == "true" and admitted_head == "a"*40:
                    fixture.assert_called_once_with("example/matrixone", "a"*40, "fixture-only", base=RELEASE_BASE)
                else:
                    fixture.assert_not_called()
                self.assertIn(f"route={expected}\n", (root / "outputs").read_text())
                receipt = root / "real-pr-authorization.json"
                if expected == "cache":
                    proof = json.loads(receipt.read_text())
                    self.assertEqual((proof["schema"], proof["base_ref"], proof["head_sha"]),
                                     (2, RELEASE_BASE, "a"*40))
                else:
                    self.assertFalse(receipt.exists())

    def test_exact_measured_jobs_and_coverage_handoff(self):
        # Hash the complete three-job body from the measured immutable pin,
        # after undoing the production entry wiring and display-only diagnostics.
        path = ROOT/".github/workflows/coverage-ut.yaml"
        src = path.read_text()
        # Reconstruct the old job wrapper only to preserve the pre-existing measured-body digest.
        worker = (ROOT/".github/workflows/coverage-worker-cs.yaml").read_text()
        steps = "    - id: reader\n" + worker.split("    - id: reader\n", 1)[1]
        # The bootstrap is exercised separately. Restore its measured version
        # here so the digest still guards every UT and artifact step.
        bootstrap = "      run: bash .standby/scripts/coverage_cs_build_tools.sh\n"
        self.assertEqual(steps.count(bootstrap), 1)
        steps = steps.replace(bootstrap, (
            "      run: |\n"
            "        set -euo pipefail\n"
            "        if command -v gcc && command -v g++ && command -v make && command -v pkg-config && command -v cmake && command -v python; then\n"
            "          echo 'Build dependencies are already available.'\n"
            "        else\n"
            "          sudo apt-get -o Acquire::ForceIPv4=true update\n"
            "          sudo apt-get -o Acquire::ForceIPv4=true install -y build-essential cmake pkg-config python3 python-is-python3\n"
            "        fi\n"
        ))
        # Undo only the review-time diagnostic reordering for the digest;
        # production writes the compact summary before optional analysis.
        marker_summary = (
            '          python3 -I "$RUNNER_TEMP/coverage-summarize-ut-report.py" "${report}" \\\n'
            '            --compact-output "$RUNNER_TEMP/ut-failure-compact.txt" | tee -a "${summary}" || true\n'
        )
        old_summary = ('          python "$GITHUB_WORKSPACE/.ci-workflow/scripts/summarize_ut_report.py" '
                       '"${report}" | tee -a "${summary}" || true\n')
        analysis_start = "    - name: Analyze and summarize failed coverage UT\n"
        diagnostics_start = "    - name: Collect coverage UT runner diagnostics\n"
        before, after = steps.split(analysis_start, 1)
        analysis, after = after.split(diagnostics_start, 1)
        self.assertEqual(analysis.count(marker_summary), 1)
        summary_slot = "          fi\n        fi\n"
        self.assertEqual(analysis.count(summary_slot), 1)
        analysis = analysis.replace(marker_summary, "").replace(summary_slot, "          fi\n"+old_summary+"        fi\n")
        steps = before + analysis_start + analysis + diagnostics_start + after
        # Ignore diagnostic-only instrumentation, not the smoke command or
        # its mandatory pass assertions, when comparing the measured body.
        phase_setup = '        phase_failure="${RUNNER_TEMP}/ut-coverage-phase-failure.txt"\n        phase_name=configure_coverage\n'
        self.assertEqual(steps.count(phase_setup), 1)
        before, after = steps.split(phase_setup, 1)
        steps = before + after.split('        trap \'record_unreported_phase_failure "$?"\' EXIT\n', 1)[1]
        record = '          if [ "${phase_status}" != \'0\' ]; then\n            record_phase_failure "${phase_status}"\n          fi\n'
        self.assertEqual(steps.count(record), 1)
        steps = steps.replace(record, '').replace('} >> "${phase_failure}"', '} > "${phase_failure}"')
        for phase in ("select_coverage_group", "configure_native_flags", "launch_coverage_suite"):
            marker_phase = f"        phase_name={phase}\n"
            self.assertEqual(steps.count(marker_phase), 1)
            steps = steps.replace(marker_phase, "")
        # The failure step may print diagnostics, but its condition, timeout,
        # placement and final exit must match the measured worker.
        marker = "    - name: Fail on coverage UT failure\n"
        before_failure, failure = steps.split(marker, 1)
        next_step = "    - name: Upload persistent tool recovery evidence\n"
        failure, after_failure = failure.split(next_step, 1)
        header, commands = failure.split("      run: |\n", 1)
        self.assertTrue(commands.endswith("        exit 1\n"))
        steps = before_failure + marker + header + "      run: exit 1\n" + next_step + after_failure
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
