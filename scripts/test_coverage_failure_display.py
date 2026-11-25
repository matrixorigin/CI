import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CoverageFailureDisplayTest(unittest.TestCase):
    def run_failure_step(self, temp):
        workflow = (ROOT / ".github/workflows/coverage-worker-cs.yaml").read_text()
        step = workflow.split("    - name: Fail on coverage UT failure\n", 1)[1]
        step = step.split("    - name: Upload persistent tool recovery evidence\n", 1)[0]
        script = textwrap.dedent(step.split("      run: |\n", 1)[1])
        return subprocess.run(["bash", "-e", "-c", script], env={**os.environ, "RUNNER_TEMP": str(temp)},
                              text=True, capture_output=True, check=False)

    def test_red_step_prints_failed_test_and_reason_from_summary(self):
        package = "github.com/matrixorigin/matrixone/pkg/vm/engine/tae/db/test"
        events = [
            {"Action": "output", "Package": package, "Test": "TestDelete3", "Output":
             "\x1b[31mpanic: runtime error: invalid memory address or nil pointer dereference\n"},
            {"Action": "fail", "Package": package, "Test": "TestDelete3"},
            {"Action": "fail", "Package": package},
            {"Action": "output", "Package": "another/package", "Test": "TestAssert",
             "Output": "    a_test.go:56: Expected: committed; Actual: rolled back\n"},
            {"Action": "fail", "Package": "another/package", "Test": "TestAssert"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            report = temp / "report.json"
            report.write_text("\n".join(map(json.dumps, events)) + "\n")
            (temp / "ut-coverage-status.json").write_text('{"result":"failed","reason":"command_exit"}')
            subprocess.run([sys.executable, str(ROOT / "scripts/summarize_ut_report.py"), str(report),
                            "--compact-output", str(temp / "ut-failure-compact.txt")],
                           check=True, capture_output=True, text=True)
            result = self.run_failure_step(temp)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"FAILED {package} / TestDelete3", result.stdout)
        self.assertIn("panic: runtime error: invalid memory address or nil pointer dereference", result.stdout)
        self.assertIn("a_test.go:56: Expected: committed; Actual: rolled back", result.stdout)
        self.assertNotIn(f"FAILED {package} / <package>", result.stdout)
        self.assertNotIn("\x1b", result.stdout)

    def test_red_step_reports_a_preparation_failure_without_a_test_report(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / "ut-coverage-status.json").write_text('{"result":"failed","reason":"coverage_phase_failure"}')
            (temp / "ut-coverage-phase-failure.txt").write_text("phase=prepare_cgo status=2\nnot for the console\n")
            result = self.run_failure_step(temp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Coverage UT phase: prepare_cgo", result.stdout)
        self.assertIn("Coverage UT status: coverage_phase_failure", result.stdout)
        self.assertNotIn("not for the console", result.stdout)

    def test_red_step_prefixes_and_bounds_log_output(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / "ut-failure-compact.txt").write_text("::error::inert\r\n" * 30)
            result = self.run_failure_step(temp)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("Coverage UT: ::error::inert"), 16)
        self.assertNotIn("\r", result.stdout)
        self.assertFalse(any(line.startswith("::") for line in result.stdout.splitlines()))
