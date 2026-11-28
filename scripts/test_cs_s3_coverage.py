"""Production CS Coverage contracts; no network or real credentials."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


DIRECTORY = Path(__file__).parents[1] / ".github/workflows"
ENTRY = (DIRECTORY / "coverage-ut.yaml").read_text()
WORKFLOW = (DIRECTORY / "coverage-worker-cs.yaml").read_text()
STANDBY = (DIRECTORY / "coverage-ut-standby.yaml").read_text()
STACKS = ("mty", "mnl2", "jed", "ruh")
CONFIGS = tuple(f"CS_S3_{stack.upper()}_CONFIG" for stack in STACKS)


def step_script(step, source=None):
    if source is None:
        source = ENTRY if step == "check_coverage_secrets" else WORKFLOW
        if step == "eligibility":
            source = (DIRECTORY / "coverage-ut-pre-cutover.yaml").read_text()
    marker = re.search(rf"^( +)- id: {step}$", source, re.M)
    block = re.split(rf"\n(?:{marker[1]}- |  [\w-]+:)", source[marker.end():], maxsplit=1)[0]
    return textwrap.dedent(re.split(r"\n +run: \|\n", block, maxsplit=1)[1])


class CSS3CoverageTest(unittest.TestCase):
    def gate(self, configs, *, member="1", label="0"):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = dict(PATH=os.environ["PATH"], GITHUB_OUTPUT=str(output), IN_ORG=member, SAFE_LABEL=label)
            env.update({name: "opaque-test-value" for name in configs})
            result = subprocess.run(["bash", "-c", step_script("check_coverage_secrets")],
                                    env=env, capture_output=True, text=True)
            self.assertNotIn("opaque-test-value", result.stdout + result.stderr)
            return result.returncode, output.read_text()

    def test_all_environments_required(self):
        self.assertEqual(self.gate(CONFIGS), (0, "ready=true\n"))
        for missing in CONFIGS:
            with self.subTest(missing=missing):
                code, output = self.gate([name for name in CONFIGS if name != missing])
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "ready=false\n")
        self.assertNotEqual(self.gate([], member="0", label="1")[0], 0)
        self.assertEqual(self.gate([], member="0"), (0, "ready=false\n"))

    def test_normal_pr_trust_gate_preserved(self):
        for member, label, ready, allowed in (("0", "0", "true", False), ("1", "0", "true", True),
                                             ("0", "1", "true", True), ("1", "0", "false", False)):
            with self.subTest(member=member, label=label, ready=ready), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                result = subprocess.run(["bash", "-c", step_script("eligibility")], capture_output=True,
                                        env=dict(PATH=os.environ["PATH"], GITHUB_OUTPUT=str(output),
                                                 IN_ORG=member, SAFE_LABEL=label, SECRETS_READY=ready))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(output.read_text(), f"allowed={str(allowed).lower()}\n")

    def test_shared_pool_and_pr_source_without_experiment_inputs(self):
        self.assertIn("""'["cs-node-cache-7c14g"]'""", WORKFLOW)
        self.assertIn("""'["cs-node-cache-7c14g","cs-{0}-node-cache-all"]'""", WORKFLOW)
        self.assertIn("source: ${{ needs.check_coverage_eligibility.outputs.head_repository }}", ENTRY)
        self.assertIn("repository: ${{ inputs.source }}", WORKFLOW)
        self.assertIn("head: ${{ needs.check_coverage_eligibility.outputs.head_sha }}", ENTRY)
        self.assertIn("ref: ${{ inputs.head }}", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"inputs\.(canary_\w+|runner_label)|weilu-runner-test|pulls/325\d+|minio-source")
        consumer = WORKFLOW
        self.assertNotRegex(consumer, r"secrets\.(TEST_S3FS_(ALIYUN|QCLOUD)|S3\w+)")
        self.assertIn("COVERAGE_UT_PACKAGE_PARALLELISM: '7'", WORKFLOW)
        self.assertIn('COVERAGE_GROUP: ${{ inputs.group }}', WORKFLOW)

    def test_runner_selects_only_its_own_storage(self):
        for stack in (*STACKS, "unknown"):
            for missing in (False, True):
                for field in ("CS_S3_ENV", "TEST_S3FS_CS"):
                    with self.subTest(stack=stack, missing=missing, field=field):
                        expression = re.search(rf"{field}: \$\{{\{{(.*?)\}}\}}", WORKFLOW, re.S)[1]
                        expression = " ".join(expression.split())
                        expression = re.sub(r"startsWith\(runner.name, '([^']+)'\)",
                                            lambda m: str(f"cs-{stack}-large-7c14g-runner".startswith(m[1])), expression)
                        for env in STACKS:
                            value = "" if missing and env == stack else env
                            expression = expression.replace(f"secrets.CS_S3_{env.upper()}_CONFIG", repr(value))
                        expression = expression.replace("&&", " and ").replace("||", " or ")
                        expected = "" if stack == "unknown" or (missing and field == "TEST_S3FS_CS") else stack
                        self.assertEqual(eval(expression, {"__builtins__": {}}), expected)

    def test_runtime_rejects_missing_config_and_isolates_attempts(self):
        script = step_script("coverage_ut").split('coverage_profile="', 1)[0]
        for stack, config, ok in [(s, "opaque-test-value", True) for s in STACKS] + [("unknown", "value", False), ("jed", "", False)]:
            for attempt in ("1", "2"):
                with self.subTest(stack=stack, config=bool(config), attempt=attempt), tempfile.TemporaryDirectory() as directory:
                    summary = Path(directory) / "summary"
                    env = dict(PATH=os.environ["PATH"], RUNNER_TEMP=directory,
                               GITHUB_WORKSPACE=directory, GITHUB_STEP_SUMMARY=str(summary),
                               GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT=attempt, CS_CACHE_ENV=stack, CS_S3_ENV=stack, TEST_S3FS_CS=config,
                               COVERAGE_GROUP="0", GOMAXPROCS="7", COVERAGE_UT_PACKAGE_PARALLELISM="7",
                               endpoint="legacy", TEST_S3FS_ALIYUN="legacy", TEST_S3FS_QCLOUD="legacy")
                    result = subprocess.run(["bash", "-c", script + '\ntest -z "${endpoint+x}${TEST_S3FS_ALIYUN+x}${TEST_S3FS_QCLOUD+x}"'],
                                            env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, ok)
                    if ok:
                        self.assertIn(f"ci-ut/123-{attempt}", summary.read_text())
                        self.assertNotIn(config, summary.read_text() + result.stdout + result.stderr)

    def test_release_storage_uses_shanghai_and_the_authorized_head(self):
        job = STANDBY.split("\n  release-storage-coverage:\n")[1].split("\n  merge-coverage-groups:\n")[0]
        self.assertIn("runs-on: amd64-mo-shanghai-ut-safe", job)
        self.assertIn("github.base_ref == '4.2-dev' && inputs.enable_4_2_dev", job)
        self.assertIn("needs.check_coverage_eligibility.outputs.allowed == 'true'", job)
        self.assertIn("ref: ${{ needs.check_coverage_eligibility.outputs.head_sha }}", job)
        self.assertLess(job.index("Verify Shanghai runner isolation"), job.index("Checkout authorized MatrixOne head"))
        self.assertLess(job.index("coverage_pr_entry.py\n        grep"), job.index("Checkout authorized MatrixOne head"))
        self.assertIn("profile: ${{ steps.profile.outputs.artifact-id }}", job)
        self.assertIn("artifact-ids: ${{ needs.release-storage-coverage.outputs.profile }}", STANDBY)

    def test_release_provider_coverage_requires_actual_provider_passes(self):
        script = step_script("test_storage_providers", STANDBY)
        package = "github.com/matrixorigin/matrixone/pkg/fileservice"
        passed = [dict(Action="pass", Package=package, Test=name) for name in (
            "TestObjectStorages/oss_eu/storage/aliyun/basic",
            "TestObjectStorages/TencentCOS/qcloud/basic",
            "TestS3FSFromSpecs/oss_eu/storage/streaming_write",
            "TestS3FSFromSpecs/TencentCOS/streaming_write",
        )]
        oss_spec = "name=oss eu/storage,endpoint=https://oss.aliyuncs.invalid,bucket=fixture\r\n"
        cos_spec = "name=TencentCOS,endpoint=https://cos.myqcloud.invalid,bucket=fixture"
        disk_only = [dict(Action="pass", Package=package, Test="TestObjectStorages/test/disk/basic")]
        cos_skipped = passed[:-1] + [dict(passed[-1], Action="skip")]
        wrong_fixture = passed[:-1] + [dict(passed[-1], Test="TestS3FSFromSpecs/test/streaming_write")]
        for name, missing, test_exit, events, expected in (
                ("both providers passed", None, 0, passed, 0),
                ("missing OSS config", "TEST_S3FS_ALIYUN", 0, passed, 1),
                ("missing COS config", "TEST_S3FS_QCLOUD", 0, passed, 1),
                ("Go test failed", None, 7, passed, 7),
                ("disk only", None, 0, disk_only, 1),
                ("only COS passed", None, 0, passed[1::2], 1),
                ("unrelated streaming fixture passed", None, 0, wrong_fixture, 1),
                ("COS streaming skipped", None, 0, cos_skipped, 1)):
            with self.subTest(name=name), \
                 tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "events").write_text("\n".join(json.dumps(event) for event in events))
                go = root / "go"
                go.write_text("#!/bin/bash\n"
                              '[[ "$TEST_S3FS_ALIYUN" == "$OSS_SPEC" && "$TEST_S3FS_QCLOUD" == "$COS_SPEC" ]] || exit 8\n'
                              '[[ -z "${TEST_S3FS_CS+x}${endpoint+x}" ]] || exit 9\n'
                              'printf "%s\\n" "$@" > "$RUNNER_TEMP/args"\n'
                              'cat "$RUNNER_TEMP/events"\n'
                              'exit "$TEST_EXIT"\n')
                go.chmod(0o755)
                env = dict(PATH=f"{root}:{os.environ['PATH']}", RUNNER_TEMP=tmp,
                           TEST_S3FS_ALIYUN=oss_spec, TEST_S3FS_QCLOUD=cos_spec,
                           OSS_SPEC=oss_spec, COS_SPEC=cos_spec, TEST_EXIT=str(test_exit),
                           TEST_S3FS_CS="unexpected", endpoint="unexpected")
                if missing:
                    env[missing] = ""
                result = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if not missing:
                    args = (root / "args").read_text().splitlines()
                    self.assertEqual(args[-1], "./pkg/fileservice")
                    self.assertIn("-covermode=set", args)
                else:
                    self.assertFalse((root / "args").exists())

    def test_release_merge_requires_and_includes_provider_coverage(self):
        require = step_script("validate_release_storage", STANDBY)
        for status, profile, expected in (
                ("success", "123", True), ("success", "", False),
                ("skipped", "123", False), ("failure", "123", False)):
            with self.subTest(status=status, profile=profile):
                result = subprocess.run(["bash", "-c", require], capture_output=True,
                                        env=dict(PATH=os.environ["PATH"], STORAGE_RESULT=status, STORAGE_PROFILE=profile))
                self.assertEqual(result.returncode == 0, expected)

        download = STANDBY.split("    - name: Download the selected release provider profile\n", 1)[1]
        download = download.split("    - id: merge_profiles\n", 1)[0]
        # The pinned action nests downloads by artifact ID unless flattening is enabled.
        self.assertRegex(download, r"(?m)^        merge-multiple: true$")
        storage_dir = re.search(r"(?m)^        path: (.+)$", download)[1]
        merge = step_script("merge_profiles", STANDBY)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("coverage_group_collect.py", "coverage_two_groups_merge.py"):
                (scripts / name).touch()
            go = root / "go"
            go.write_text("#!/bin/bash\nexit 0\n")
            go.chmod(0o755)
            for folder, name in (("selected/profiles/0", "group0"), ("selected/profiles/1", "group1"),
                                 (storage_dir, "oss-cos")):
                profile = root / folder / "ut-coverage.out"
                profile.parent.mkdir(parents=True)
                profile.write_text(f"mode: set\n{name}:1.1,1.2 1 1\n")
            env = dict(PATH=f"{root}:{os.environ['PATH']}", RUNNER_TEMP=tmp, EXPECTED_HEAD_SHA="a"*40,
                       GROUP_COUNT="2", GITHUB_RUN_ID="42")
            for required in ("false", "true"):
                with self.subTest(required=required):
                    result = subprocess.run(["bash", "-c", merge], cwd=tmp, capture_output=True,
                                            env={**env, "RELEASE_STORAGE_REQUIRED": required})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    contents = (root / "ut-coverage.out").read_text()
                    self.assertEqual(contents.count("mode: set"), 1)
                    self.assertIn("group0:", contents)
                    self.assertIn("group1:", contents)
                    self.assertEqual("oss-cos:" in contents, required == "true")
            (root / storage_dir / "ut-coverage.out").unlink()
            result = subprocess.run(["bash", "-c", merge], cwd=tmp, capture_output=True,
                                    env={**env, "RELEASE_STORAGE_REQUIRED": "true"})
            self.assertNotEqual(result.returncode, 0)

    def test_smoke_requires_pass_events_and_preserves_safe_failure_details(self):
        script = step_script("coverage_ut").split("\nphase_start cs_s3_smoke\n", 1)[1]
        script = script.split("\nphase_name=launch_coverage_suite", 1)[0]
        script = """set -euo pipefail
cgo_cflags= cgo_ldflags=
phase_failure="$RUNNER_TEMP/failure"
phase_end() { printf 'phase=cs_s3_smoke status=%s\\n' "$1" > "$phase_failure"; }
""" + script
        names = ("TestCSStoragePrefixScopedInitialization", "TestObjectStorages/cs-s3/minio/basic",
                 "TestS3FSFromSpecs/cs-s3/streaming_write")
        passes = [dict(Action="pass", Test=name) for name in names]
        fixture_test = "TestS3FSFromSpecs/cs-s3"
        cleanup_output = ("    object_storage_arguments_test.go:111: "
                          "CS S3 remove fixture object failed (HTTP 0, code DeadlineExceeded, type *url.Error)\n")
        cases = (
            ("success", passes, 0, 0, []),
            ("missing", passes[:-1], 0, 1, [f"smoke_missing_test_pass={names[-1]}"]),
            ("assertion", [
                dict(Action="output", Test=names[1], Output="assertion failed: credential-marker"),
                dict(Action="fail", Test=names[1]),
                dict(Action="fail", Test=names[1]),
                dict(Action="fail", Test="TestObjectStorages/cs-s3/minio"),
                dict(Action="fail", Test=names[1] + "/credential-marker"),
                dict(Action="fail", Test="TestCITestS3FixtureSafety"),
                dict(Action="fail"),
            ], 7, 7, ["smoke_failed_test=TestCITestS3FixtureSafety", f"smoke_failed_test={names[1]}"]),
            ("cleanup_deadline", [
                dict(Action="output", Test=names[-1], Output="PUT credential-marker: context deadline exceeded"),
                dict(Action="fail", Test=names[-1]),
                dict(Action="output", Test=fixture_test, Output=cleanup_output),
                dict(Action="output", Output="panic: test timed out after 5m0s\n"),
            ], 7, 7, ["smoke_operation=remove_fixture_object smoke_http_status=0 smoke_error_code=DeadlineExceeded",
                      f"smoke_failed_test={names[-1]}"]),
            ("missing_bucket", [dict(Action="output", Test=fixture_test, Output=
                "CS S3 list fixture prefix failed (HTTP 404, code NoSuchBucket, type minio.ErrorResponse)\n")],
             7, 7, ["smoke_operation=list_fixture_prefix smoke_http_status=404 smoke_error_code=NoSuchBucket"]),
            ("go_timeout", [dict(Action="output", Output="panic: test timed out after 5m0s\n")],
             7, 7, ["smoke_signal=go_test_timeout"]),
            ("untrusted_output", [
                dict(Action="output", Test=names[0], Output=cleanup_output),
                dict(Action="output", Test=fixture_test, Output=cleanup_output.rstrip() + " credential-marker"),
            ], 7, 7, ["smoke_signal=unknown"]),
        )
        for mode, events, go_exit, expected_exit, diagnostics in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "events").write_text("\n".join(map(json.dumps, events)) + "\n")
                go = root / "go"
                go.write_text('#!/bin/sh\ncat "$RUNNER_TEMP/events"\nexit "$TEST_EXIT"\n')
                go.chmod(0o755)
                env = dict(PATH=f"{root}:{os.environ['PATH']}", RUNNER_TEMP=directory,
                           TEST_EXIT=str(go_exit), GITHUB_STEP_SUMMARY=str(root / "summary"))
                result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                self.assertNotIn("credential-marker", result.stdout + result.stderr)
                if diagnostics:
                    self.assertEqual((root / "failure").read_text().splitlines(),
                                     [f"phase=cs_s3_smoke status={expected_exit}", *diagnostics])
                if mode == "missing":
                    self.assertIn(f"Required CS S3 smoke test did not pass: {names[-1]}", result.stdout)
                elif expected_exit:
                    self.assertEqual(result.stdout.splitlines(), ["CS S3 smoke failure", *diagnostics])

    def test_required_s3_tests_cannot_be_missing_skipped_or_failed(self):
        script = step_script("coverage_ut")
        script = 'if [[ "${COVERAGE_GROUP}" == \'0\' ]]; then' + script.split('if [[ "${COVERAGE_GROUP}" == \'0\' ]]; then', 1)[1].split("phase_start compact_profile", 1)[0]
        names = ("TestObjectStorages/cs-s3/minio/basic", "TestS3FSFromSpecs/cs-s3/streaming_write")
        for action in ("pass", "skip", "fail", "missing"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                report = Path(directory) / "report.json"
                report.write_text("\n".join(json.dumps(dict(Action=action if i else "pass", Test=name,
                                                          Package="github.com/matrixorigin/matrixone/pkg/fileservice"))
                                           for i, name in enumerate(names) if not (i and action == "missing")))
                result = subprocess.run(["bash", "-c", script], capture_output=True,
                                        env=dict(PATH=os.environ["PATH"], coverage_report=str(report), COVERAGE_GROUP="0"))
                self.assertEqual(result.returncode == 0, action == "pass")

    def test_skip_manifest_uploaded_for_success_and_failure(self):
        block = WORKFLOW.split("- id: skip_manifest\n", 1)[1].split("- id: upload_coverage_report\n", 1)[0]
        self.assertIn("always() && !cancelled()", block)
        self.assertIn("steps.coverage_ut.outcome == 'success' || steps.coverage_ut.outcome == 'failure'", block)
        self.assertIn("steps.skip_manifest.outputs.available == 'true'", block)
        self.assertIn("coverage_skip_manifest.py", block)
        self.assertIn("ut-coverage-skip-manifest.json", block)


if __name__ == "__main__":
    unittest.main()
