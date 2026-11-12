import pathlib
import subprocess
import tempfile
import unittest

from scripts.run_coverage_packages import EMBED, groups, run


class CoveragePackageSchedulingTest(unittest.TestCase):
    def test_partition_preserves_every_package_once(self):
        packages = ["example/a", EMBED, "example/b"]
        plan = groups(packages)
        self.assertEqual(plan, [[EMBED], ["example/a", "example/b"]])
        self.assertCountEqual([p for group in plan for p in group], packages)
        self.assertEqual(groups(["example/a"]), [["example/a"]])
        for invalid in ([], [EMBED, EMBED]):
            with self.assertRaises(ValueError):
                groups(invalid)

    def test_failure_does_not_skip_remaining_packages_or_hide_failure(self):
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first), tempfile.TemporaryDirectory() as directory:
                profile = pathlib.Path(directory) / "combined.out"
                commands = []

                def execute(command):
                    commands.append(command)
                    part = pathlib.Path(next(v.split("=", 1)[1] for v in command if v.startswith("-coverprofile=")))
                    part.write_text(f"mode: set\nexample/file.go:1.1,2.1 1 {len(commands)-1}\n")
                    return subprocess.CompletedProcess(command, 1 if fail_first and len(commands) == 1 else 0)

                status = run([EMBED, "example/a", "example/b"], profile, "example/instrumented", 8, execute)
                self.assertEqual(status, int(fail_first))
                self.assertEqual(len(commands), 2)
                self.assertEqual(commands[0][-1:], [EMBED])
                self.assertEqual(commands[1][-2:], ["example/a", "example/b"])
                for command in commands:
                    self.assertIn("-timeout=20m", command)
                    self.assertIn("-coverpkg=example/instrumented", command)
                    self.assertEqual(command[command.index("-p")+1], "8")
                self.assertEqual(profile.read_text().count("mode: set"), 1)
                self.assertEqual(len(profile.read_text().splitlines()), 3)

    def test_missing_profile_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run([EMBED], pathlib.Path(directory)/"result", "scope", 8,
                         lambda command: subprocess.CompletedProcess(command, 0))
            self.assertEqual(result, 1)

    def test_existing_profile_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/"result"
            path.write_text("sentinel")
            with self.assertRaises(ValueError):
                run([EMBED], path, "scope", 8)
            self.assertEqual(path.read_text(), "sentinel")


if __name__ == "__main__":
    unittest.main()
