import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/coverage-ut.yaml"
RUNNER = ROOT / "scripts/run_coverage_ut.py"


class CoverageBuildModeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = self.root / "go-events.jsonl"
        self.workflow = WORKFLOW.read_text()
        start = self.workflow.index("          # Select coverage build mode")
        end = self.workflow.index("          phase_start discover_packages", start)
        self.mode = textwrap.dedent(self.workflow[start:end])
        executable = self.root / "go"
        executable.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path
                import sys

                args = sys.argv[1:]
                values = {name: os.environ.get(name) for name in ['GOEXPERIMENT', 'GOAMD64']}
                with Path(os.environ['FAKE_GO_EVENTS']).open('a') as output:
                    output.write(json.dumps({'args': args, **values}) + '\\n')
                if args[0] == 'env':
                    if os.environ.get('REJECT_MODE') == 'true':
                        sys.exit(37)
                    metadata = {'GOVERSION': 'test-compiler', 'GOOS': 'test-os', 'GOARCH': 'test-arch'}
                    for name in args[1:]:
                        print(metadata.get(name, values.get(name) or ''))
                elif args[0] == 'list':
                    print('test/package')
                elif args[0] == 'test':
                    print(json.dumps({'Action': 'pass', 'Package': 'test/package'}))
                else:
                    sys.exit(29)
                """
            )
        )
        executable.chmod(0o755)

    def run_mode(self, enabled=None, inherited=None, reject=False, runner=False):
        self.events.unlink(missing_ok=True)
        env = os.environ.copy()
        for name in ["GOEXPERIMENT", "GOAMD64", "SIMD_COVERAGE", "REJECT_MODE"]:
            env.pop(name, None)
        env.update(inherited or {})
        env["PATH"] = str(self.root) + os.pathsep + env["PATH"]
        env["FAKE_GO_EVENTS"] = str(self.events)
        if enabled is not None:
            env["SIMD_COVERAGE"] = enabled
        if reject:
            env["REJECT_MODE"] = "true"
        status = self.root / "status.json"
        report = self.root / "report.json"
        if runner:
            command = shlex.join(
                [
                    sys.executable,
                    str(RUNNER),
                    "--report", str(report),
                    "--progress", str(self.root / "progress.txt"),
                    "--status", str(status),
                    "--timeout-diagnostics", str(self.root / "timeout.txt"),
                    "--timeout-seconds", "5",
                    "--", "go", "test",
                ]
            )
        else:
            command = "go test"
        result = subprocess.run(
            ["bash", "-eu", "-c", self.mode + "\ngo list -mod=readonly ./...\n" + command],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        return result, events, status, report

    def assert_mode(self, events, experiment, architecture):
        self.assertEqual([event["args"][0] for event in events], ["env", "list", "test"])
        self.assertEqual(
            events[0]["args"],
            ["env", "GOVERSION", "GOOS", "GOARCH", "GOEXPERIMENT", "GOAMD64"],
        )
        for event in events:
            self.assertEqual(event["GOEXPERIMENT"], experiment)
            self.assertEqual(event["GOAMD64"], architecture)

    def test_input_defaults_off_and_precedes_all_go_phases(self):
        declaration = re.search(
            r"^      simd_coverage:\n((?:^        .*\n)+)", self.workflow, re.MULTILINE
        )
        self.assertIsNotNone(declaration)
        self.assertRegex(declaration[1], r"(?m)^        type: boolean$")
        self.assertRegex(declaration[1], r"(?m)^        default: false$")
        self.assertIn("SIMD_COVERAGE: ${{ inputs.simd_coverage }}", self.workflow)
        selected = self.workflow.index(self.mode.splitlines()[0].strip())
        for marker in [
            "phase_start discover_packages",
            "phase_start build_coverage_scope",
            "phase_start prepare_cgo",
            'python "$GITHUB_WORKSPACE/.ci-workflow/scripts/run_coverage_ut.py"',
        ]:
            self.assertLess(selected, self.workflow.index(marker))

    def test_omitted_and_disabled_preserve_environment(self):
        for enabled in [None, "false"]:
            for inherited in [{}, {"GOEXPERIMENT": "nosimd", "GOAMD64": "v2"}]:
                with self.subTest(enabled=enabled, inherited=inherited):
                    result, events, _, _ = self.run_mode(enabled, inherited)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_mode(events, inherited.get("GOEXPERIMENT"), inherited.get("GOAMD64"))

    def test_enabled_selects_production_mode(self):
        result, events, _, _ = self.run_mode(
            "true", {"GOEXPERIMENT": "nosimd", "GOAMD64": "v1"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_mode(events, "simd", "v3")

    def test_real_runner_inherits_selected_or_preserved_environment(self):
        for enabled, experiment, architecture in [("true", "simd", "v3"), ("false", "nosimd", "v2")]:
            with self.subTest(enabled=enabled):
                result, events, status, report = self.run_mode(
                    enabled, {"GOEXPERIMENT": "nosimd", "GOAMD64": "v2"}, runner=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_mode(events, experiment, architecture)
                self.assertEqual(json.loads(status.read_text())["result"], "passed")
                self.assertEqual(json.loads(report.read_text())["Action"], "pass")

    def test_rejected_mode_stops_before_discovery_and_runner(self):
        result, events, status, report = self.run_mode("true", reject=True, runner=True)
        self.assertEqual(result.returncode, 37)
        self.assertEqual([event["args"][0] for event in events], ["env"])
        self.assertFalse(status.exists())
        self.assertFalse(report.exists())

    def test_validator_enforces_build_mode_contracts(self):
        validator = (ROOT / ".github/workflows/check-action-file.yaml").read_text()
        self.assertIn("      - 'scripts/test_coverage_build_mode.py'", validator)
        self.assertIn("      - 'scripts/run_coverage_ut.py'", validator)
        self.assertIn("run: python3 scripts/test_coverage_build_mode.py -v", validator)


if __name__ == "__main__":
    unittest.main()
