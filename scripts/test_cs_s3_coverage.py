"""Contract checks for the CS S3 Coverage path; no network or credentials."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).parents[1] / ".github/workflows/coverage-ut.yaml"
CS_CONFIGS = tuple(f"CS_S3_{stack}_CONFIG" for stack in ("MTY", "MNL2", "JED", "RUH"))
CLOUD_CONFIGS = (
    "TEST_S3FS_ALIYUN", "TEST_S3FS_QCLOUD", "S3ENDPOINT", "S3REGION",
    "S3APIKEY", "S3APISECRET", "S3BUCKET",
)


class CSS3CoverageTest(unittest.TestCase):
    def gate(self, configs, *, runner="cs-large-7c14g", trusted=True, canary=False, local=False, jed=False):
        workflow = WORKFLOW.read_text()
        block = workflow.split("      - id: check_coverage_secrets\n", 1)[1].split("      - id: eligibility\n", 1)[0]
        script = textwrap.dedent(block.split("        run: |\n", 1)[1])
        for key, value in {
            "runner_label": runner,
            "canary_pr_32523": str(canary).lower(),
            "canary_local_minio": str(local).lower(),
            "canary_cs_s3_jed": str(jed).lower(),
        }.items():
            script = script.replace("${{ inputs." + key + " }}", value)
        self.assertNotIn("${{", script)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output),
                   "IN_ORG": "1" if trusted else "0", "SAFE_LABEL": "0"}
            env.update({name: "opaque-test-value" for name in configs})
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            self.assertNotIn("opaque-test-value", result.stdout + result.stderr)
            return result.returncode, output.read_text(), result.stdout

    def test_all_cs_environments_required_for_shared_pool(self):
        code, output, _ = self.gate(CS_CONFIGS)
        self.assertEqual(code, 0)
        self.assertIn("ready=true", output)
        for missing in CS_CONFIGS:
            with self.subTest(missing=missing):
                code, output, log = self.gate([name for name in CS_CONFIGS if name != missing])
                self.assertNotEqual(code, 0)
                self.assertIn("ready=false", output)
                self.assertIn(missing, log)

    def test_cloud_credentials_do_not_substitute_for_cs(self):
        self.assertNotEqual(self.gate(CLOUD_CONFIGS)[0], 0)

    def test_untrusted_pr_does_not_require_secrets(self):
        code, output, _ = self.gate([], trusted=False)
        self.assertEqual(code, 0)
        self.assertIn("ready=false", output)

    def test_secret_free_local_canary(self):
        code, output, _ = self.gate([], canary=True, local=True)
        self.assertEqual(code, 0)
        self.assertIn("ready=true", output)

    def test_legacy_tke_and_real_cloud_canary_keep_original_requirements(self):
        for args in ({"runner": "amd64-tke"}, {"canary": True}):
            with self.subTest(args=args):
                self.assertIn("ready=true", self.gate(CLOUD_CONFIGS, **args)[1])
                self.assertIn("ready=false", self.gate(CS_CONFIGS, **args)[1])

    def test_jed_canary_requires_only_dedicated_jed_secret(self):
        args = {"runner": "cs-jed-large-7c14g", "trusted": False, "jed": True}
        self.assertIn("ready=true", self.gate(["CS_S3_JED_CONFIG"], **args)[1])
        self.assertNotEqual(self.gate(CLOUD_CONFIGS, **args)[0], 0)
        self.assertNotEqual(self.gate([], **args)[0], 0)

    def test_jed_canary_pins_context_source_and_runner(self):
        workflow = WORKFLOW.read_text()
        guard = workflow.split("      - id: jed_context\n", 1)[1].split("      - name: Validate pinned canary context\n", 1)[0]
        for required in ("matrixorigin/matrixone", "workflow_dispatch", "refs/heads/weilu-runner-test",
                         "cs-jed-large-7c14g", "pulls/32590", "Ariznawlll/matrixone",
                         "67e3cf40c8445fb429a7a20c0a67b95dec065155"):
            self.assertIn(required, guard)
        for mode in ("canary_pr_32523", "canary_local_minio"):
            self.assertIn("test '${{ inputs." + mode + " }}' = 'false'", guard)
        secrets_block = workflow.split("      - id: check_coverage_secrets\n", 1)[1].split("        run: |\n", 1)[0]
        for name in CLOUD_CONFIGS:
            self.assertIn(name + ": ${{ !inputs.canary_cs_s3_jed && secrets." + name + " || '' }}", secrets_block)

    def test_jed_context_rejects_other_refs_sources_and_modes(self):
        block = WORKFLOW.read_text().split("      - id: jed_context\n", 1)[1].split("      - name: Validate pinned canary context\n", 1)[0]
        original = textwrap.dedent(block.split("        run: |\n", 1)[1])
        values = {"github.repository": "matrixorigin/matrixone", "github.event_name": "workflow_dispatch",
                  "github.ref": "refs/heads/weilu-runner-test", "inputs.runner_label": "cs-jed-large-7c14g",
                  "inputs.canary_pr_32523": "false", "inputs.canary_local_minio": "false"}
        cases = [({}, "true", True), ({}, "false", False)]
        for key in values:
            cases.append(({key: "unexpected"}, "true", False))
        for changes, api_result, accepted in cases:
            with self.subTest(changes=changes, api_result=api_result), tempfile.TemporaryDirectory() as directory:
                script = original
                for key, value in dict(values, **changes).items():
                    script = script.replace("${{ " + key + " }}", value)
                self.assertNotIn("${{", script)
                output = Path(directory) / "output"
                result = subprocess.run(["bash", "-c", "gh() { echo " + api_result + "; };\n" + script],
                                        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted)
                self.assertEqual(output.exists() and "validated=true" in output.read_text(), accepted)

    def test_runner_selects_only_its_own_secret(self):
        workflow = WORKFLOW.read_text()
        expression = next(line for line in workflow.splitlines() if "TEST_S3FS_CS: ${{" in line)
        mappings = re.findall(r"startsWith\(runner.name, '([^']+)'\) && secrets\.(\w+)", expression)
        self.assertEqual(mappings, [(f"cs-{stack.lower()}-", f"CS_S3_{stack}_CONFIG")
                                    for stack in ("MTY", "MNL2", "JED", "RUH")])
        self.assertTrue(expression.endswith("|| '' }}"))
        self.assertIn('test -n "${TEST_S3FS_CS:-}"', workflow)

    def test_missing_or_skipped_cs_tests_cannot_pass(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("TestObjectStorages/cs-s3/minio/basic", workflow)
        self.assertIn("TestS3FSFromSpecs/cs-s3/streaming_write", workflow)
        self.assertIn('select(.Action == "pass"', workflow)
        self.assertIn('export MO_CI_S3_PREFIX="ci-ut/${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"', workflow)
        self.assertIn("unset endpoint region apikey apisecret bucket TEST_S3FS_ALIYUN TEST_S3FS_QCLOUD", workflow)


if __name__ == "__main__":
    unittest.main()
