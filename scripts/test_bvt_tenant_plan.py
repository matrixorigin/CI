import json
import tempfile
import unittest
from pathlib import Path

from scripts.bvt_tenant_plan import PolicyEntry, build_plan, load_policy, write_plan


class PlannerFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.case_root = self.root / "cases"
        self.case_root.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def case(self, relative_path, content="select 1;"):
        path = self.case_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path.resolve()

    def selected(self, *relative_paths):
        path = self.root / "selected-files.txt"
        path.write_text(
            "".join(f"{(self.case_root / relative).resolve()}\n" for relative in relative_paths)
        )
        return path


class TestBuildPlan(PlannerFixture):
    def test_assigns_whole_directories_and_preserves_selection(self):
        self.case("alpha/a.sql", "x")
        self.case("alpha/b.sql", "xx")
        self.case("beta/a.sql", "xxx")
        self.case("global/a.sql", "xxxx")
        selected = self.selected("alpha/a.sql", "alpha/b.sql", "beta/a.sql", "global/a.sql")
        policy = {
            "alpha": PolicyEntry("parallel", "safe"),
            "beta": PolicyEntry("parallel", "safe"),
            "global": PolicyEntry("serial-after", "global state"),
        }

        plan = build_plan(self.case_root, selected, policy, workers=2)

        self.assertEqual(plan["serial_after"], [str((self.case_root / "global").resolve())])
        self.assertEqual(
            sorted(path for worker in plan["workers"] for path in worker["directories"]),
            sorted(
                [
                    str((self.case_root / "alpha").resolve()),
                    str((self.case_root / "beta").resolve()),
                ]
            ),
        )
        self.assertEqual(plan["selected_script_count"], 4)
        self.assertEqual(plan["selected_directory_count"], 3)

    def test_rejects_partial_top_level_directory_selection(self):
        self.case("alpha/a.sql")
        self.case("alpha/b.sql")
        selected = self.selected("alpha/a.sql")

        with self.assertRaisesRegex(ValueError, "partial directory selection.*alpha"):
            build_plan(
                self.case_root,
                selected,
                {"alpha": PolicyEntry("parallel", "safe")},
                workers=2,
            )

    def test_rejects_selected_file_outside_case_root(self):
        outside = self.root / "outside.sql"
        outside.write_text("select 1;")
        selected = self.root / "selected-files.txt"
        selected.write_text(f"{outside.resolve()}\n")

        with self.assertRaisesRegex(ValueError, "outside case root"):
            build_plan(self.case_root, selected, {}, workers=2)

    def test_unknown_directory_defaults_to_unreviewed_serial_after(self):
        self.case("new_suite/a.sql")
        selected = self.selected("new_suite/a.sql")

        plan = build_plan(self.case_root, selected, {}, workers=2)

        self.assertEqual(plan["serial_after"], [str((self.case_root / "new_suite").resolve())])
        self.assertEqual(
            plan["directories"],
            [
                {
                    "name": "new_suite",
                    "path": str((self.case_root / "new_suite").resolve()),
                    "phase": "serial-after",
                    "reason": "unreviewed directory defaults to serial-after",
                    "reviewed": False,
                    "script_count": 1,
                    "weight_bytes": len("select 1;"),
                    "worker": None,
                }
            ],
        )

    def test_worker_count_must_be_between_one_and_four(self):
        self.case("alpha/a.sql")
        selected = self.selected("alpha/a.sql")
        policy = {"alpha": PolicyEntry("parallel", "safe")}

        for workers in (0, 5):
            with self.subTest(workers=workers):
                with self.assertRaisesRegex(ValueError, "workers must be between 1 and 4"):
                    build_plan(self.case_root, selected, policy, workers=workers)

    def test_longest_first_balancing_is_deterministic(self):
        self.case("alpha/a.sql", "a" * 10)
        self.case("beta/a.sql", "b" * 7)
        self.case("gamma/a.sql", "g" * 6)
        selected = self.selected("alpha/a.sql", "beta/a.sql", "gamma/a.sql")
        policy = {
            name: PolicyEntry("parallel", "safe") for name in ("alpha", "beta", "gamma")
        }

        plan = build_plan(self.case_root, selected, policy, workers=2)

        self.assertEqual(
            plan["workers"],
            [
                {
                    "index": 0,
                    "weight_bytes": 10,
                    "directories": [str((self.case_root / "alpha").resolve())],
                },
                {
                    "index": 1,
                    "weight_bytes": 13,
                    "directories": [
                        str((self.case_root / "beta").resolve()),
                        str((self.case_root / "gamma").resolve()),
                    ],
                },
            ],
        )

    def test_write_plan_emits_directory_only_include_files(self):
        self.case("before/a.sql")
        self.case("alpha/a.sql")
        self.case("after/a.test")
        selected = self.selected("before/a.sql", "alpha/a.sql", "after/a.test")
        policy = {
            "before": PolicyEntry("serial-before", "ordered"),
            "alpha": PolicyEntry("parallel", "safe"),
            "after": PolicyEntry("serial-after", "global"),
        }
        output_dir = self.root / "plan"

        plan = build_plan(self.case_root, selected, policy, workers=2)
        write_plan(plan, output_dir)

        self.assertEqual(
            (output_dir / "serial-before.include").read_text().strip(),
            str((self.case_root / "before").resolve()),
        )
        self.assertEqual(
            (output_dir / "worker-0.include").read_text().strip(),
            str((self.case_root / "alpha").resolve()),
        )
        self.assertEqual((output_dir / "worker-1.include").read_text(), "")
        self.assertEqual(
            (output_dir / "serial-after.include").read_text().strip(),
            str((self.case_root / "after").resolve()),
        )
        for include_file in output_dir.glob("*.include"):
            content = include_file.read_text()
            self.assertNotIn(".sql", content)
            self.assertNotIn(".test", content)
        written_plan = json.loads((output_dir / "plan.json").read_text())
        self.assertEqual(written_plan, plan)
        inventory = (output_dir / "inventory.tsv").read_text().splitlines()
        self.assertEqual(
            inventory[0],
            "directory\tphase\tworker\treviewed\tscript_count\tweight_bytes\treason",
        )
        self.assertEqual(len(inventory), 4)


class TestLoadPolicy(PlannerFixture):
    def test_rejects_duplicate_directory_keys(self):
        policy_path = self.root / "policy.json"
        policy_path.write_text(
            """{
  "schema_version": 1,
  "directories": {
    "alpha": {"phase": "parallel", "reason": "one"},
    "alpha": {"phase": "serial-after", "reason": "two"}
  }
}
"""
        )

        with self.assertRaisesRegex(ValueError, "duplicate JSON key: alpha"):
            load_policy(policy_path)

    def test_rejects_invalid_phase(self):
        policy_path = self.root / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "directories": {
                        "alpha": {"phase": "sometimes", "reason": "invalid"}
                    },
                }
            )
        )

        with self.assertRaisesRegex(ValueError, "invalid phase.*sometimes"):
            load_policy(policy_path)

    def test_repository_policy_has_expected_directory_counts(self):
        policy = load_policy(Path(__file__).with_name("bvt_tenant_policy.json"))
        counts = {
            phase: sum(entry.phase == phase for entry in policy.values())
            for phase in ("serial-before", "parallel", "serial-after")
        }

        self.assertEqual(len(policy), 72)
        self.assertEqual(
            counts,
            {"serial-before": 5, "parallel": 29, "serial-after": 38},
        )


if __name__ == "__main__":
    unittest.main()
