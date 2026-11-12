import json
import os
import pathlib
import subprocess
import sys
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

                def execute(command, *, stderr):
                    commands.append(command)
                    stderr.write(b"warning: no packages being tested depend on example/unrelated\n")
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
                diagnostics = profile.with_suffix(".stderr.log").read_text()
                self.assertEqual(diagnostics.count("warning: no packages"), 2)
                self.assertNotIn("warning:", profile.read_text())

    def test_missing_profile_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run([EMBED], pathlib.Path(directory)/"result", "scope", 8,
                         lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
            self.assertEqual(result, 1)

    def test_existing_profile_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/"result"
            path.write_text("sentinel")
            with self.assertRaises(ValueError):
                run([EMBED], path, "scope", 8)
            self.assertEqual(path.read_text(), "sentinel")

    def test_existing_diagnostics_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/"result.out"
            diagnostics = path.with_suffix(".stderr.log")
            diagnostics.write_text("existing diagnostic evidence")
            with self.assertRaises(FileExistsError):
                run([EMBED], path, "scope", 8)
            self.assertEqual(diagnostics.read_text(), "existing diagnostic evidence")

    def test_compiler_warning_does_not_corrupt_supervised_json_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fake_go = root/"go"
            fake_go.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys\n"
                "profile = next(x.split('=',1)[1] for x in sys.argv if x.startswith('-coverprofile='))\n"
                "pathlib.Path(profile).write_text('mode: set\\nexample/a.go:1.1,2.1 1 1\\n')\n"
                "print('warning: no packages being tested depend on example/a', file=sys.stderr)\n"
                "print(json.dumps(dict(Action='pass', Package=sys.argv[-1])))\n"
            )
            fake_go.chmod(0o700)
            profile = root/"result.out"
            report = root/"report.json"
            status = root/"status.json"
            result = subprocess.run(
                [sys.executable, str(pathlib.Path(__file__).with_name('run_coverage_ut.py')),
                 '--report', str(report), '--progress', str(root/'progress.txt'),
                 '--status', str(status), '--timeout-diagnostics', str(root/'timeout.txt'),
                 '--timeout-seconds', '10', '--',
                 sys.executable, str(pathlib.Path(__file__).with_name('run_coverage_packages.py')),
                 '--profile', str(profile), '--coverpkg', 'example/a', '--parallel', '8',
                 EMBED, 'example/a'],
                env={**os.environ, 'PATH': str(root)+os.pathsep+os.environ['PATH']},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            events = [json.loads(line) for line in report.read_text().splitlines()]
            self.assertEqual([e['Package'] for e in events], [EMBED, 'example/a'])
            self.assertTrue(all(e['Action']=='pass' for e in events))
            self.assertEqual(json.loads(status.read_text())['result'], 'passed')
            self.assertEqual(profile.with_suffix('.stderr.log').read_text().count('warning:'), 2)


if __name__ == "__main__":
    unittest.main()
