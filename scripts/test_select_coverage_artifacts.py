import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from select_coverage_artifacts import (
    SelectionError,
    discover_candidates,
    link_selected,
    select_candidates,
    selection_document,
)


SCRIPT = Path(__file__).with_name("select_coverage_artifacts.py")


class CoverageArtifactSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def artifact(
        self,
        producer: str,
        generation: str,
        attempt: int,
        files: dict[str, str],
    ) -> None:
        directory = (
            self.downloads
            / f"{producer}-generation-{generation}-attempt-{attempt}"
        )
        directory.mkdir()
        for relative_path, content in files.items():
            path = directory / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def complete_generation(self, generation: str, attempt: int = 1) -> None:
        self.artifact(
            "ut-coverage", generation, attempt, {"ut-coverage.out": "ut"}
        )
        self.artifact(
            "bvt-coverage-compose",
            generation,
            attempt,
            {
                "bvt-compose.out": "compose",
                "bvt-compose-manifest.json": "{}",
            },
        )
        self.artifact(
            "bvt-coverage-pessimistic",
            generation,
            attempt,
            {
                "bvt-pessimistic.out": "pessimistic",
                "bvt-pessimistic-manifest.json": "{}",
            },
        )

    def select(self, generation: str = ""):
        return select_candidates(discover_candidates(self.downloads), generation)

    def test_selects_highest_attempt_per_producer(self):
        self.complete_generation("run-1")
        self.artifact(
            "ut-coverage", "run-1", 3, {"ut-coverage.out": "fresh-ut"}
        )
        self.artifact(
            "bvt-coverage-compose",
            "run-1",
            2,
            {
                "bvt-compose.out": "fresh-compose",
                "bvt-compose-manifest.json": "{\"attempt\": 2}",
            },
        )

        selected = self.select("run-1")
        output = self.root / "selected"
        link_selected(selected, output)

        self.assertEqual(selected["ut-coverage"].attempt, 3)
        self.assertEqual(selected["bvt-coverage-compose"].attempt, 2)
        self.assertEqual(selected["bvt-coverage-pessimistic"].attempt, 1)
        self.assertEqual((output / "ut-coverage.out").read_text(), "fresh-ut")
        self.assertEqual(
            (output / "ut-coverage.out").stat().st_ino,
            (selected["ut-coverage"].path / "ut-coverage.out").stat().st_ino,
        )
        self.assertEqual(
            (output / "bvt-compose.out").read_text(), "fresh-compose"
        )
        self.assertEqual(
            selection_document(selected)["producers"]["ut-coverage"]["attempt"],
            3,
        )

    def test_expected_generation_ignores_newer_unrelated_generation(self):
        self.complete_generation("wanted", attempt=1)
        self.complete_generation("other", attempt=9)

        selected = self.select("wanted")

        self.assertEqual(
            {candidate.generation for candidate in selected.values()}, {"wanted"}
        )

    def test_without_expected_generation_requires_one_complete_generation(self):
        self.complete_generation("first")
        self.complete_generation("second")

        with self.assertRaisesRegex(SelectionError, "exactly one complete"):
            self.select()

    def test_selects_from_githubs_maximum_run_attempt_set(self):
        for attempt in range(1, 52):
            self.complete_generation("run-1", attempt=attempt)

        selected = self.select("run-1")

        self.assertEqual(
            {candidate.attempt for candidate in selected.values()}, {51}
        )

    def test_missing_producer_fails_closed(self):
        self.artifact("ut-coverage", "run-1", 1, {"ut-coverage.out": "ut"})
        self.artifact(
            "bvt-coverage-compose",
            "run-1",
            1,
            {"bvt-compose-manifest.json": "{}"},
        )

        with self.assertRaisesRegex(SelectionError, "does not have all three"):
            self.select("run-1")

    def test_newest_attempt_does_not_fall_back_to_stale_files(self):
        self.complete_generation("run-1")
        self.artifact("ut-coverage", "run-1", 2, {"diagnostic.txt": "missing"})

        selected = self.select("run-1")
        output = self.root / "selected"
        link_selected(selected, output)

        self.assertFalse((output / "ut-coverage.out").exists())
        self.assertTrue((output / "diagnostic.txt").exists())

    def test_duplicate_relative_paths_fail_instead_of_overwriting(self):
        self.complete_generation("run-1")
        self.artifact(
            "ut-coverage", "run-1", 2, {"shared.txt": "from-ut"}
        )
        self.artifact(
            "bvt-coverage-compose",
            "run-1",
            2,
            {"shared.txt": "from-compose"},
        )

        with self.assertRaisesRegex(SelectionError, "order-dependent overwrite"):
            link_selected(self.select("run-1"), self.root / "selected")

    def test_cli_writes_auditable_selection_manifest(self):
        self.complete_generation("run-1", attempt=4)
        output = self.root / "selected"
        manifest = self.root / "selection.json"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--input",
                str(self.downloads),
                "--output",
                str(output),
                "--expected-generation",
                "run-1",
                "--manifest",
                str(manifest),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selected ut-coverage", result.stdout)
        document = json.loads(manifest.read_text())
        self.assertEqual(document["generation"], "run-1")
        self.assertEqual(document["producers"]["ut-coverage"]["attempt"], 4)

    def test_cli_failure_emits_a_github_error_annotation(self):
        self.artifact("ut-coverage", "run-1", 1, {"ut-coverage.out": "ut"})

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--input",
                str(self.downloads),
                "--output",
                str(self.root / "selected"),
                "--expected-generation",
                "run-1",
                "--manifest",
                str(self.root / "selection.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "::error title=Coverage artifact selection::", result.stderr
        )


if __name__ == "__main__":
    unittest.main()
