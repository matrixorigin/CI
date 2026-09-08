import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


SCRIPT = Path(__file__).with_name("run_coverage_ut.py")


class RunCoverageUTTest(unittest.TestCase):
    def run_wrapper(self, child_source: str, timeout: int = 5, extra_env=None):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        root = Path(directory)
        report = root / "report.json"
        progress = root / "progress.txt"
        status = root / "status.json"
        timeout_diagnostics = root / "timeout.txt"
        timing = root / "timing.txt"
        command = [
            sys.executable,
            "-u",
            "-c",
            textwrap.dedent(child_source),
        ]
        env = os.environ.copy()
        env.update(extra_env or {})
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--report",
                str(report),
                "--progress",
                str(progress),
                "--status",
                str(status),
                "--timeout-diagnostics",
                str(timeout_diagnostics),
                "--phase-timing",
                str(timing),
                "--timeout-seconds",
                str(timeout),
                "--heartbeat-seconds",
                "0.05",
                "--",
                *command,
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return result, report, progress, status, timeout_diagnostics, timing

    def test_success_records_last_test_and_status(self):
        result, report, progress, status, timeout_diagnostics, timing = self.run_wrapper(
            """
            import json
            print(json.dumps({'Action': 'run', 'Package': 'pkg/a', 'Test': 'TestFast'}), flush=True)
            print(json.dumps({'Action': 'pass', 'Package': 'pkg/a', 'Test': 'TestFast', 'Elapsed': 0.2}), flush=True)
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(status.read_text())["result"], "passed")
        self.assertIn("test=TestFast", progress.read_text())
        self.assertIn("phase=coverage_ut status=passed", timing.read_text())
        self.assertFalse(timeout_diagnostics.exists())
        self.assertTrue(report.exists())

    def test_timeout_saves_reason_and_progress_before_killing_group(self):
        result, report, progress, status, timeout_diagnostics, timing = self.run_wrapper(
            """
            import json
            import time
            print(json.dumps({'Action': 'run', 'Package': 'pkg/stuck', 'Test': 'TestWait'}), flush=True)
            time.sleep(30)
            """,
            timeout=1,
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        status_data = json.loads(status.read_text())
        self.assertEqual(status_data["result"], "timeout")
        self.assertEqual(status_data["reason"], "internal_deadline")
        self.assertIn("package=pkg/stuck", progress.read_text())
        timeout_text = timeout_diagnostics.read_text()
        self.assertIn("TestWait", timeout_text)
        self.assertIn("coverage_process_pid=", timeout_text)
        self.assertIn("phase=coverage_ut status=timeout", timing.read_text())
        self.assertTrue(report.exists())

    def test_failed_command_keeps_exit_reason_distinct_from_timeout(self):
        result, _report, _progress, status, _timeout_diagnostics, timing = self.run_wrapper(
            """
            import json
            print(json.dumps({'Action': 'fail', 'Package': 'pkg/fail', 'Test': 'TestBroken'}), flush=True)
            raise SystemExit(17)
            """
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        status_data = json.loads(status.read_text())
        self.assertEqual(status_data["result"], "failed")
        self.assertEqual(status_data["reason"], "command_exit")
        self.assertIn("phase=coverage_ut status=failed", timing.read_text())

    def test_timeout_kills_grandchild_after_group_leader_exits(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        pid_file = Path(directory) / "grandchild.pid"
        result, _report, _progress, status, _timeout_diagnostics, _timing = self.run_wrapper(
            """
            import json
            import os
            import signal
            import subprocess
            import sys
            import time
            grandchild = subprocess.Popen([
                sys.executable,
                '-c',
                'import signal,time; signal.signal(signal.SIGQUIT, signal.SIG_IGN); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',
            ])
            with open(os.environ['GRANDCHILD_PID_FILE'], 'w') as stream:
                stream.write(str(grandchild.pid))
            print(json.dumps({'Action': 'run', 'Package': 'pkg/child', 'Test': 'TestChild'}), flush=True)
            time.sleep(30)
            """,
            timeout=1,
            extra_env={"GRANDCHILD_PID_FILE": str(pid_file)},
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual(json.loads(status.read_text())["result"], "timeout")
        grandchild_pid = int(pid_file.read_text())
        for _ in range(100):
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            try:
                state = Path(f"/proc/{grandchild_pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            self.fail(f"grandchild {grandchild_pid} survived process-group cleanup")
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    unittest.main()
