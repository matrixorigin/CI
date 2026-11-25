import json
import os
from pathlib import Path
import shutil
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

    def test_compact_failure_is_ready_when_optional_tool_is_interrupted(self):
        workflow = (ROOT / ".github/workflows/coverage-worker-cs.yaml").read_text()
        step = workflow.split("    - name: Analyze and summarize failed coverage UT\n", 1)[1]
        step = step.split("    - name: Collect coverage UT runner diagnostics\n", 1)[0]
        script = textwrap.dedent(step.split("      run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            workspace = temp / "workspace"
            (workspace / ".standby").mkdir(parents=True)
            (workspace / ".standby/scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
            copy_step = "    - name: Preserve the trusted failure summary helper\n"
            source_step = "    - name: Checkout MatrixOne source\n"
            self.assertLess(workflow.index(copy_step), workflow.index(source_step))
            copy_cmd = workflow.split(copy_step, 1)[1].split("      run: ", 1)[1].splitlines()[0]
            subprocess.run(["bash", "-ec", copy_cmd], cwd=workspace,
                           env={**os.environ, "RUNNER_TEMP": str(temp)}, check=True)
            # actions/checkout clears the root here because it has no root .git.
            shutil.rmtree(workspace)
            workspace.mkdir()
            (temp / "ut-coverage-status.json").write_text('{"result":"failed"}')
            (temp / "ut-coverage-report.json").write_text(json.dumps({
                "Action": "fail", "Package": "example/pkg", "Test": "TestFailed"
            }) + "\n")
            # Simulate termination while attempting the optional install.
            go_stub = 'go() { [[ -s "$RUNNER_TEMP/ut-failure-compact.txt" ]] || exit 97; exit 124; }\n'
            interrupted = subprocess.run(
                ["bash", "-e", "-c", go_stub + script],
                env={**os.environ, "RUNNER_TEMP": str(temp), "GITHUB_WORKSPACE": str(workspace),
                     "GITHUB_STEP_SUMMARY": str(temp / "summary.md")},
                text=True, capture_output=True, check=False,
            )
            result = self.run_failure_step(temp)
        self.assertEqual(interrupted.returncode, 124, interrupted.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED example/pkg / TestFailed", result.stdout)

    def test_actual_phase_producer_reports_timeouts_and_pre_report_failures(self):
        workflow = (ROOT / ".github/workflows/coverage-worker-cs.yaml").read_text()
        step = workflow.split("    - id: coverage_ut\n", 1)[1]
        script = textwrap.dedent(step.split("      run: |\n", 1)[1].split("    - name: Print failed coverage UT", 1)[0])
        setup = script.split('cd "$GITHUB_WORKSPACE"', 1)[0]
        phases = "phase_start() {" + script.split("phase_start() {", 1)[1].split("\nphase_start discover_packages", 1)[0]
        for phase, code, command in (
                ("cs_s3_smoke", 7, "phase_start cs_s3_smoke; phase_end 7; exit 7"),
                ("prepare_cgo", 124, "phase_start prepare_cgo; phase_end 124 30; exit 124"),
                ("cs_s3_smoke", 1, "phase_start cs_s3_smoke; phase_end 0; exit 1"),
                ("configure_coverage", 1, "exit 1")):
            with self.subTest(phase=phase, code=code), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                fixtures = ('phase_timing="$RUNNER_TEMP/timing"\n'
                            'timeout_diagnostics="$RUNNER_TEMP/timeout"\n'
                            'cgo_log="$RUNNER_TEMP/cgo"\npackage_error="$RUNNER_TEMP/packages"\n')
                failure = subprocess.run(["bash", "-ec", setup+fixtures+phases+command],
                                         env={**os.environ, "RUNNER_TEMP": str(temp)},
                                         capture_output=True, text=True, check=False)
                self.assertEqual(failure.returncode, code, failure.stderr)
                record = (temp / "ut-coverage-phase-failure.txt").read_text()
                self.assertTrue(record.startswith(f"phase={phase} status={code}\n"), record)
                self.assertIn(f"Coverage UT phase: {phase}", self.run_failure_step(temp).stdout)

    def test_red_step_prefixes_and_bounds_log_output(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / "ut-failure-compact.txt").write_text("::error::inert\r\n" * 30)
            result = self.run_failure_step(temp)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("Coverage UT: ::error::inert"), 16)
        self.assertNotIn("\r", result.stdout)
        self.assertFalse(any(line.startswith("::") for line in result.stdout.splitlines()))
