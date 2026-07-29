import json
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path):
    ruby = r"""
require "json"
require "yaml"
puts JSON.generate(YAML.load_file(ARGV.fetch(0)))
"""
    result = subprocess.run(
        ["ruby", "-e", ruby, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class WorkflowContract:
    def __init__(self, test_case, filename, job_name):
        self.test_case = test_case
        self.document = load_yaml(REPOSITORY_ROOT / ".github" / "workflows" / filename)
        workflow_on = self.document.get("on") or self.document.get("true")
        self.inputs = workflow_on["workflow_call"]["inputs"]
        self.steps = self.document["jobs"][job_name]["steps"]

    def step(self, name):
        matches = [step for step in self.steps if step.get("name") == name]
        self.test_case.assertEqual(
            len(matches),
            1,
            f"expected one step named {name!r}, found {len(matches)}",
        )
        return matches[0]

    def assert_inputs(self):
        expected = {
            "tenant_parallel_enabled": {
                "description": "Run directory-safe BVT cases in tenant workers",
                "required": False,
                "type": "boolean",
                "default": False,
            },
            "tenant_parallel_workers": {
                "description": "Tenant worker count (1-4)",
                "required": False,
                "type": "number",
                "default": 2,
            },
            "ci_ref": {
                "description": "matrixorigin/CI ref containing tenant BVT scripts",
                "required": False,
                "type": "string",
                "default": "main",
            },
        }
        for name, value in expected.items():
            self.test_case.assertIn(name, self.inputs)
            self.test_case.assertEqual(self.inputs[name], value)

    def assert_ci_checkout(self):
        step = self.step("Checkout tenant-parallel BVT scripts")
        self.test_case.assertEqual(step["if"], "${{ inputs.tenant_parallel_enabled }}")
        self.test_case.assertEqual(
            step["uses"],
            "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
        )
        self.test_case.assertEqual(step["with"]["repository"], "matrixorigin/CI")
        self.test_case.assertEqual(step["with"]["ref"], "${{ inputs.ci_ref }}")
        self.test_case.assertEqual(step["with"]["path"], ".ci/tenant-parallel")


class TestComposeWorkflow(unittest.TestCase):
    def setUp(self):
        self.workflow = WorkflowContract(
            self,
            "e2e-compose-parallel.yaml",
            "multi-CN-bvt-docker-compose-proxy",
        )

    def test_inputs_and_ci_checkout(self):
        self.workflow.assert_inputs()
        self.workflow.assert_ci_checkout()

    def test_start_step_has_opt_in_and_fallback_branches(self):
        run = self.workflow.step("Start BVT Test")["run"]

        self.assertIn(
            "if [ '${{ inputs.tenant_parallel_enabled }}' = 'true' ]; then",
            run,
        )
        self.assertIn(
            'bash "$GITHUB_WORKSPACE/.ci/tenant-parallel/scripts/run_bvt_tenant_parallel.sh"',
            run,
        )
        self.assertIn('--case-root "$GITHUB_WORKSPACE/test/distributed/cases"', run)
        self.assertNotIn("--resource-dir", run)
        self.assertIn("--workers '${{ inputs.tenant_parallel_workers }}'", run)
        self.assertIn('else\n  bash "${bvt_runner}"', run)

    def test_artifact_contains_tenant_output(self):
        path = self.workflow.step("Upload Compose BVT execution log")["with"]["path"]

        self.assertIn("${{ runner.temp }}/bvt-compose.log", path)
        self.assertIn("${{ runner.temp }}/bvt-tenant-compose", path)


class TestStandaloneWorkflow(unittest.TestCase):
    def setUp(self):
        self.workflow = WorkflowContract(
            self,
            "e2e-standalone-parallel.yaml",
            "pessimistic-bvt-linux-x86",
        )

    def test_inputs_and_ci_checkout(self):
        self.workflow.assert_inputs()
        self.workflow.assert_ci_checkout()

    def test_start_step_has_opt_in_resource_and_fallback_branches(self):
        run = self.workflow.step("Start BVT Test")["run"]

        self.assertIn(
            "if [ '${{ inputs.tenant_parallel_enabled }}' = 'true' ]; then",
            run,
        )
        self.assertIn(
            'bash "$GITHUB_WORKSPACE/.ci/tenant-parallel/scripts/run_bvt_tenant_parallel.sh"',
            run,
        )
        self.assertIn('--case-root "$GITHUB_WORKSPACE/head/test/distributed/cases"', run)
        self.assertIn(
            '--resource-dir "$GITHUB_WORKSPACE/head/test/distributed/resources"',
            run,
        )
        self.assertIn("--workers '${{ inputs.tenant_parallel_workers }}'", run)
        self.assertIn('else\n  bash "${bvt_runner}"', run)

    def test_artifact_contains_tenant_output(self):
        path = self.workflow.step("Upload Launch + Pessimistic BVT execution log")[
            "with"
        ]["path"]

        self.assertIn("${{ runner.temp }}/bvt-pessimistic.log", path)
        self.assertIn("${{ runner.temp }}/bvt-tenant-pessimistic", path)


if __name__ == "__main__":
    unittest.main()
