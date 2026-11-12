"""Production CS Coverage contracts; no network or real credentials."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = (Path(__file__).parents[1] / ".github/workflows/coverage-ut.yaml").read_text()
STACKS = ("mty", "mnl2", "jed", "ruh")
CONFIGS = tuple(f"CS_S3_{stack.upper()}_CONFIG" for stack in STACKS)


def step_script(step):
    block = re.split(r"\n(?:      - |  [\w-]+:)",
                     WORKFLOW.split(f"      - id: {step}\n", 1)[1], maxsplit=1)[0]
    return textwrap.dedent(block.split("        run: |\n", 1)[1])


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
        self.assertIn("runs-on: cs-large-7c14g", WORKFLOW)
        for field, value in (("PR_AUTHOR", "user.login"), ("PR_URL", "url"),
                             ("repository", "head.repo.full_name"), ("ref", "head.sha")):
            self.assertIn(field + ": ${{ github.event.pull_request." + value + " }}", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"inputs\.(canary_\w+|runner_label)|weilu-runner-test|pulls/325\d+|minio-source")
        self.assertNotRegex(WORKFLOW, r"secrets\.(TEST_S3FS_(ALIYUN|QCLOUD)|S3\w+)")
        self.assertIn('COVERAGE_UT_PACKAGE_PARALLELISM: "8"', WORKFLOW)

    def test_runner_selects_only_its_own_storage(self):
        for stack in (*STACKS, "unknown"):
            for missing in (False, True):
                for field in ("CS_S3_ENV", "TEST_S3FS_CS"):
                    with self.subTest(stack=stack, missing=missing, field=field):
                        expression = next(line.split("${{", 1)[1].split("}}", 1)[0]
                                          for line in WORKFLOW.splitlines() if f"{field}: ${{{{" in line)
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
                    env = dict(PATH=os.environ["PATH"], GITHUB_WORKSPACE=directory, GITHUB_STEP_SUMMARY=str(summary),
                               GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT=attempt, CS_S3_ENV=stack, TEST_S3FS_CS=config,
                               endpoint="legacy", TEST_S3FS_ALIYUN="legacy", TEST_S3FS_QCLOUD="legacy")
                    result = subprocess.run(["bash", "-c", script + '\ntest -z "${endpoint+x}${TEST_S3FS_ALIYUN+x}${TEST_S3FS_QCLOUD+x}"'],
                                            env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, ok)
                    if ok:
                        self.assertIn(f"ci-ut/123-{attempt}", summary.read_text())
                        self.assertNotIn(config, summary.read_text() + result.stdout + result.stderr)

    def test_required_s3_tests_cannot_be_missing_skipped_or_failed(self):
        script = step_script("coverage_ut")
        script = "for required in 'TestObjectStorages/" + script.split("for required in 'TestObjectStorages/", 1)[1].split("phase_start compact_profile", 1)[0]
        names = ("TestObjectStorages/cs-s3/minio/basic", "TestS3FSFromSpecs/cs-s3/streaming_write")
        for action in ("pass", "skip", "fail", "missing"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                report = Path(directory) / "report.json"
                report.write_text("\n".join(json.dumps(dict(Action=action if i else "pass", Test=name,
                                                          Package="github.com/matrixorigin/matrixone/pkg/fileservice"))
                                           for i, name in enumerate(names) if not (i and action == "missing")))
                result = subprocess.run(["bash", "-c", script], capture_output=True,
                                        env=dict(PATH=os.environ["PATH"], coverage_report=str(report)))
                self.assertEqual(result.returncode == 0, action == "pass")

    def test_skip_manifest_uploaded_for_success_and_failure(self):
        block = WORKFLOW.split("      - id: skip_manifest\n", 1)[1].split("      - id: upload_coverage_report\n", 1)[0]
        self.assertIn("always() && !cancelled()", block)
        self.assertNotIn("outcome == 'failure'", block)
        self.assertIn("coverage_skip_manifest.py", block)
        self.assertIn("ut-coverage-skip-manifest.json", block)


if __name__ == "__main__":
    unittest.main()
