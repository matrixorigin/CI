"""Exercise the trusted inline workflow using synthetic UT artifacts."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yaml"


def run_packager(workspace, runner_temp, limit):
    workflow = WORKFLOW.read_text()
    section = workflow.split("      - name: Bound UT diagnostic artifact\n", 1)[1]
    script = section.split("          python3 - <<'PY'\n", 1)[1].split(
        "\n          PY", 1
    )[0]
    env = os.environ.copy()
    env.update(
        GITHUB_WORKSPACE=str(workspace),
        RUNNER_TEMP=str(runner_temp),
        UT_DIAGNOSTIC_MAX_BYTES=str(limit),
    )
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


class UTDiagnosticArtifactTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.workspace = root / "workspace"
        self.runner = root / "runner"
        self.workspace.mkdir()
        self.runner.mkdir()
        self.stage = self.runner / "ut-diagnostics"
        self.limit = 65536 + 4096

    def write(self, path, data):
        file = self.workspace / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)

    def assert_budget(self, limit):
        total = sum(p.stat().st_size for p in self.stage.rglob("*") if p.is_file())
        self.assertLessEqual(total, limit)

    def test_failures_survive_an_oversized_raw_report(self):
        self.write("ut-report/failed/result.txt", b"failed test\n")
        self.write("ut-report/failed/outputs/pkg/TestFailure", b"actual failure\n")
        self.write("ut-report/ut-stderr.log", b"stderr\n")
        self.write("ut-report/ut-checkpoint.log", b"checkpoint\n")
        self.write("ut-report/ut-report.json", b"R" * 12000)
        self.write("ut-report/top.txt", b"T" * 6000)
        run_packager(self.workspace, self.runner, self.limit)
        for path in (
            "ut-report/failed/result.txt",
            "ut-report/failed/outputs/pkg/TestFailure",
            "ut-report/ut-stderr.log",
            "ut-report/ut-checkpoint.log",
        ):
            self.assertEqual(
                (self.workspace / path).read_bytes(), (self.stage / path).read_bytes()
            )
        report = (self.stage / "ut-report/ut-report.json").read_bytes()
        self.assertIn(b"UT diagnostic file truncated", report)
        self.assertIn(
            b"ut-report/ut-report.json\toriginal_bytes=12000",
            (self.stage / "manifest.txt").read_bytes(),
        )
        self.assert_budget(self.limit)
        self.assertEqual((self.workspace / "ut-report/ut-report.json").stat().st_size, 12000)

    def test_large_failures_share_the_budget(self):
        paths = ["ut-report/failed/outputs/pkg/TestA", "ut-report/failed/outputs/pkg/TestB"]
        for path in paths:
            self.write(path, b"X" * 10000)
        run_packager(self.workspace, self.runner, self.limit)
        for path in paths:
            self.assertIn(b"UT diagnostic file truncated", (self.stage / path).read_bytes())
        self.assert_budget(self.limit)

    def test_budget_smaller_than_manifest_reserve(self):
        self.write("ut-report/failed/outputs/pkg/TestFailure", b"failure")
        run_packager(self.workspace, self.runner, 32)
        self.assert_budget(32)

    def test_large_stderr_does_not_hide_failures(self):
        self.write("ut-report/ut-stderr.log", b"E" * 12000)
        self.write("ut-report/ut-checkpoint.log", b"C" * 12000)
        self.write("ut-report/failed/outputs/pkg/TestFailure", b"actual failure")
        run_packager(self.workspace, self.runner, self.limit)
        self.assertEqual(
            (self.stage / "ut-report/failed/outputs/pkg/TestFailure").read_bytes(),
            b"actual failure",
        )
        self.assertIn(
            b"UT diagnostic file truncated",
            (self.stage / "ut-report/ut-stderr.log").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
