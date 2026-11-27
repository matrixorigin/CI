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

    def test_release_provider_test_uses_both_specs_and_surfaces_failure(self):
        script = step_script("test_storage_providers", STANDBY)
        for aliyun, qcloud, test_exit, expected in (
                ("oss-spec", "cos-spec", 0, 0), ("", "cos-spec", 0, 1),
                ("oss-spec", "", 0, 1), ("oss-spec", "cos-spec", 7, 7)):
            with self.subTest(aliyun=bool(aliyun), qcloud=bool(qcloud), test_exit=test_exit), \
                 tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                go = root / "go"
                go.write_text("#!/bin/bash\n"
                              '[[ "${TEST_S3FS_ALIYUN}" == oss-spec && "${TEST_S3FS_QCLOUD}" == cos-spec ]] || exit 8\n'
                              '[[ -z "${TEST_S3FS_CS+x}${endpoint+x}" ]] || exit 9\n'
                              'printf "%s\\n" "$@" > "$RUNNER_TEMP/args"\n'
                              'exit "$TEST_EXIT"\n')
                go.chmod(0o755)
                env = dict(PATH=f"{root}:{os.environ['PATH']}", RUNNER_TEMP=tmp,
                           TEST_S3FS_ALIYUN=aliyun, TEST_S3FS_QCLOUD=qcloud, TEST_EXIT=str(test_exit),
                           TEST_S3FS_CS="unexpected", endpoint="unexpected")
                result = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                if aliyun and qcloud:
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
                                 ("raw/release-storage", "oss-cos")):
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
            (root / "raw/release-storage/ut-coverage.out").unlink()
            result = subprocess.run(["bash", "-c", merge], cwd=tmp, capture_output=True,
                                    env={**env, "RELEASE_STORAGE_REQUIRED": "true"})
            self.assertNotEqual(result.returncode, 0)

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
