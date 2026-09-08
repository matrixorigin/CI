import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from summarize_coverage_ut import MAX_MARKDOWN_BYTES, cap_markdown, render


class SummarizeCoverageUTTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)

    def write(self, name: str, content: str) -> Path:
        path = self.directory / name
        path.write_text(content)
        return path

    def test_report_has_bounded_human_readable_sections(self):
        report = self.directory / "report.json"
        events = [
            {"Action": "run", "Package": "pkg/a", "Test": "TestSlow", "Time": "2026-01-01T00:00:00Z"},
            {"Action": "output", "Package": "pkg/a", "Test": "TestSlow", "Output": "--- FAIL: TestSlow\n" + "x" * 5000},
            {"Action": "fail", "Package": "pkg/a", "Test": "TestSlow", "Elapsed": 4.5},
            {"Action": "pass", "Package": "pkg/b", "Test": "TestFast", "Elapsed": 1.25},
        ]
        report.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        status = self.write(
            "status.json",
            json.dumps({"result": "failed", "reason": "command_exit", "exit_code": 1}),
        )
        phase = self.write("phase.txt", "phase=coverage_ut status=failed elapsed_seconds=5\n")
        progress = self.write("progress.txt", "finished package=pkg/a test=TestSlow\n")
        timeout = self.write("timeout.txt", "Coverage UT internal deadline exceeded.\n")
        phase_failure = self.directory / "phase-failure.txt"
        output = self.directory / "incident.md"

        text = render(
            type(
                "Args",
                (),
                {
                    "report": report,
                    "status": status,
                    "phase_timing": phase,
                    "progress": progress,
                    "timeout_diagnostics": timeout,
                    "phase_failure": phase_failure,
                    "max_failures": 50,
                    "max_excerpts": 20,
                    "max_slow": 30,
                },
            )()
        )
        output.write_text(text)

        self.assertIn("# Coverage UT incident report", text)
        self.assertIn("Failed packages and tests", text)
        self.assertIn("pkg/a", text)
        self.assertIn("Slowest completed tests", text)
        self.assertIn("Phase timing", text)
        self.assertIn("Recent progress", text)
        self.assertLess(len(text), 12000)
        self.assertNotIn("x" * 5000, text)

    def test_missing_inputs_still_explain_no_evidence(self):
        output = render(
            type(
                "Args",
                (),
                {
                    "report": self.directory / "missing-report.json",
                    "status": self.directory / "missing-status.json",
                    "phase_timing": self.directory / "missing-phase.txt",
                    "progress": self.directory / "missing-progress.txt",
                    "timeout_diagnostics": self.directory / "missing-timeout.txt",
                    "phase_failure": self.directory / "missing-phase-failure.txt",
                    "max_failures": 50,
                    "max_excerpts": 20,
                    "max_slow": 30,
                },
            )()
        )
        self.assertIn("**Producer status:** `unknown`", output)
        self.assertIn("No valid Go test JSON event", output)
        self.assertIn("Raw evidence", output)

    def test_auxiliary_lines_and_markdown_have_hard_limits(self):
        status = self.write("status.json", json.dumps({"result": "timeout", "reason": "fallback_deadline"}))
        report = self.directory / "report.json"
        report.write_text(
            "\n".join(
                json.dumps(
                    {
                        "Action": "output",
                        "Package": "pkg/a",
                        "Test": "TestLong",
                        "Output": "--- FAIL: TestLong\n" + "z" * 5000,
                    }
                )
                for _ in range(20)
            )
            + "\n"
        )
        phase = self.write("phase.txt", "phase=coverage_ut status=timeout\n")
        progress = self.write("progress.txt", ("`" * 3 + "x" * 100000 + "\n") * 20)
        timeout = self.write("timeout.txt", ("y" * 100000 + "\n") * 20)
        phase_failure = self.directory / "phase-failure.txt"
        output = render(
            type(
                "Args",
                (),
                {
                    "report": report,
                    "status": status,
                    "phase_timing": phase,
                    "progress": progress,
                    "timeout_diagnostics": timeout,
                    "phase_failure": phase_failure,
                    "max_failures": 50,
                    "max_excerpts": 20,
                    "max_slow": 30,
                },
            )()
        )
        self.assertLessEqual(len(output.encode("utf-8")), 64 * 1024)
        self.assertNotIn("```x", output)
        self.assertIn("Report truncated", output)

    def test_truncation_never_leaves_an_unclosed_fence_at_the_boundary(self):
        for offset in range(-12, 13):
            output = cap_markdown(
                "a" * (MAX_MARKDOWN_BYTES + offset) + "\n```text\nlate evidence\n```\n"
            )
            self.assertLessEqual(len(output.encode("utf-8")), 64 * 1024)
            self.assertEqual(output.count("```") % 2, 0, f"offset={offset}")

    def test_renderer_truncation_keeps_generated_fences_balanced(self):
        status = self.write("status.json", json.dumps({"result": "timeout"}))
        report = self.write("report.json", "")
        phase = self.write(
            "phase.txt",
            "".join(f"phase=coverage_ut status=running {'p' * 983}\n" for _ in range(66)),
        )
        progress = self.write("progress.txt", ("q" * 1023 + "\n") * 20)
        timeout = self.write("timeout.txt", "")
        phase_failure = self.write("phase-failure.txt", "")
        output = render(
            type(
                "Args",
                (),
                {
                    "report": report,
                    "status": status,
                    "phase_timing": phase,
                    "progress": progress,
                    "timeout_diagnostics": timeout,
                    "phase_failure": phase_failure,
                    "max_failures": 50,
                    "max_excerpts": 20,
                    "max_slow": 30,
                },
            )()
        )
        self.assertLessEqual(len(output.encode("utf-8")), MAX_MARKDOWN_BYTES)
        self.assertEqual(output.count("```") % 2, 0)
        self.assertIn("Report truncated", output)


if __name__ == "__main__":
    unittest.main()
