import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import collect


HEALTHY = {"Status": "exited", "ExitCode": 0, "OOMKilled": False, "Error": ""}
SQL_HIT = "github.com/matrixorigin/matrixone/pkg/frontend/mysql_cmd_executor.go:1.1,2.2 1 1\n"


class StopCNsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.compose = Path(directory.name) / "compose.yaml"
        self.compose.write_text("GOCOVERDIR=/coverage\n../../coverage:/coverage\n")

    def responses(self, *states):
        return ["first\n", "second\n", "", *(json.dumps(s) for s in states)]

    def test_normal_stop_preserves_dependencies_and_checks_both_original_ids(self):
        for _ in range(2):
            with patch.object(collect, "run", side_effect=self.responses(HEALTHY, HEALTHY)) as run:
                collect.stop_cns(self.compose)
            stop = run.call_args_list[2]
            self.assertEqual(stop.args[0][-5:], ["stop", "--timeout", "120", "cn-0", "cn-1"])
            self.assertEqual(stop.kwargs["timeout"], 180)
            self.assertEqual([call.args[0][-1] for call in run.call_args_list[3:]], ["first", "second"])
            self.assertFalse(any("down" in call.args[0] for call in run.call_args_list))

    def test_missing_or_multiple_containers_fail_before_stop(self):
        for ids in ("", "first\nsecond\n"):
            with self.subTest(ids=ids), patch.object(collect, "run", return_value=ids) as run:
                with self.assertRaisesRegex(ValueError, "expected one cn-0"):
                    collect.stop_cns(self.compose)
                self.assertEqual(run.call_count, 1)

    def test_unclean_exit_is_rejected_after_both_states_are_inspected(self):
        for change in ({"ExitCode": 137}, {"ExitCode": 2}, {"Status": "running"},
                       {"OOMKilled": True}, {"Error": "daemon failure"}, {"OOMKilled": None}):
            for failing_index in (0, 1):
                states = [dict(HEALTHY), dict(HEALTHY)]
                states[failing_index].update(change)
                with self.subTest(change=change, index=failing_index):
                    with patch.object(collect, "run", side_effect=self.responses(*states)) as run:
                        with self.assertRaisesRegex(ValueError, "did not complete cleanly"):
                            collect.stop_cns(self.compose)
                        self.assertEqual(run.call_count, 5)

    def test_stop_error_and_timeout_are_not_treated_as_normal_exit(self):
        for failure in (subprocess.CalledProcessError(1, "docker"),
                        subprocess.TimeoutExpired("docker", 180)):
            with self.subTest(failure=failure):
                with patch.object(collect, "run", side_effect=["first", "second", failure]) as run:
                    with self.assertRaises(type(failure)):
                        collect.stop_cns(self.compose)
                    self.assertEqual(run.call_count, 3)

    def test_legacy_compose_preserves_existing_cleanup_without_a_coverage_gate(self):
        self.compose.write_text("services: {}\n")
        with patch.object(collect, "run") as run:
            collect.stop_cns(self.compose)
            run.assert_not_called()


class ProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.compose = self.root / "compose.yaml"
        self.compose.write_text("GOCOVERDIR=/coverage\n../../coverage:/coverage\n")
        self.output = self.root / "bvt.out"

    def convert(self, content):
        def conversion(args, **kwargs):
            self.assertEqual(args[:4], ["go", "tool", "covdata", "textfmt"])
            self.assertEqual(kwargs["timeout"], 180)
            Path(args[-1].removeprefix("-o=")).write_text(content)
            return ""
        return conversion

    def test_publish_valid_profile_and_replace_previous_generation(self):
        for mode in ("set", "count", "atomic"):
            content = f"mode: {mode}\n" + SQL_HIT
            self.output.write_text("stale data")
            with patch.object(collect, "run", side_effect=self.convert(content)):
                collect.generate_profile(self.compose, self.root, self.output)
            self.assertEqual(self.output.read_text(), content)
            self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["bvt.out", "compose.yaml"])

    def test_missing_sql_counters_and_malformed_profiles_are_not_published(self):
        # Startup/authentication in a proxy can execute frontend code without
        # any CN SQL counters: that is the observed production counterexample.
        auth_hit = SQL_HIT.replace("mysql_cmd_executor.go", "authenticate.go")
        for content in ("", "mode: set\n", "mode: set\n" + auth_hit,
                        "mode: set\n" + SQL_HIT[:-2] + "0\n",
                        "mode: set\n" + SQL_HIT + "invalid\n"):
            with self.subTest(content=content):
                self.output.write_text("previous valid profile")
                with patch.object(collect, "run", side_effect=self.convert(content)):
                    with self.assertRaises(ValueError):
                        collect.generate_profile(self.compose, self.root, self.output)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.root.iterdir()), [self.compose])

    def test_conversion_failure_removes_old_output_and_temporary_files(self):
        self.output.write_text("previous valid profile")
        with patch.object(collect, "run", side_effect=subprocess.CalledProcessError(1, "go")):
            with self.assertRaises(subprocess.CalledProcessError):
                collect.generate_profile(self.compose, self.root, self.output)
        self.assertEqual(list(self.root.iterdir()), [self.compose])

    def test_legacy_compose_does_not_publish_stale_profile(self):
        self.compose.write_text("services: {}\n")
        self.output.write_text("previous valid profile")
        with patch.object(collect, "run") as run:
            collect.generate_profile(self.compose, self.root, self.output)
            run.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
