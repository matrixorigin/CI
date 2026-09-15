#!/usr/bin/env python3

from pathlib import Path
import re
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yaml"


def job_source(source: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)",
        source, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing job: {name}")
    return match.group(1)


def validate_summary_contract(source: str) -> str:
    summary = job_source(source, "ut-linux-x86")
    shards = job_source(source, "ut-linux-x86-shard")
    for name, job in (("summary", summary), ("shards", shards)):
        if not re.search(r"^    environment: ci$", job, re.MULTILINE):
            raise AssertionError(f"{name} must select the ci environment")
    for required in (
        "    if: ${{ always() && inputs.ut_sharded }}",
        "    needs: ut-linux-x86-shard",
        "          UT_SHARD_RESULT: ${{ needs.ut-linux-x86-shard.result }}",
    ):
        if required not in summary.splitlines():
            raise AssertionError(f"missing summary contract: {required.strip()}")
    if "continue-on-error:" in summary:
        raise AssertionError("summary must not ignore failures")
    script = re.search(r"^        run: \|\n((?:          .*\n|\n)+)", summary, re.MULTILINE)
    if script is None:
        raise AssertionError("missing summary result check")
    return textwrap.dedent(script.group(1))


class UTSummaryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")

    def test_environment_and_shard_dependency(self) -> None:
        validate_summary_contract(self.source)

    def test_missing_summary_environment_is_rejected(self) -> None:
        summary = job_source(self.source, "ut-linux-x86")
        self.assertIn("    environment: ci\n", summary)
        broken = self.source.replace(summary, summary.replace("    environment: ci\n", "", 1), 1)
        with self.assertRaisesRegex(AssertionError, "summary must select"):
            validate_summary_contract(broken)

    def test_missing_dependency_or_admission_is_rejected(self) -> None:
        for line in (
            "    needs: ut-linux-x86-shard\n",
            "    if: ${{ always() && inputs.ut_sharded }}\n",
        ):
            with self.subTest(line=line):
                with self.assertRaisesRegex(AssertionError, "missing summary contract"):
                    validate_summary_contract(self.source.replace(line, "", 1))

    def test_actual_result_check_accepts_only_success(self) -> None:
        script = validate_summary_contract(self.source)
        for result in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(result=result):
                completed = subprocess.run(
                    ["/bin/bash", "--noprofile", "--norc", "-e", "-c", script],
                    env={"UT_SHARD_RESULT": result},
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(completed.returncode == 0, result == "success", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
