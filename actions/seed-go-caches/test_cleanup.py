"""Real OS-signal and subprocess cleanup contracts; no daemon/network needed."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from test_seed import FakeSeeder, EXISTING, MISSING, seed


def scenario(root, mode):
    seed.CHECKOUT = str(root)
    seed.MODULES = str(root / "modules")
    instance = FakeSeeder(root)
    original = instance.cleanup_command
    targeted = False

    def cleanup(args, timeout):
        nonlocal targeted
        if args[0] == "docker":
            assert Path(instance.env["DOCKER_CONFIG"]).is_dir()
            return
        path = Path(args[3])
        target = path.name.startswith(".mo-seed-") and (
            mode != "module" or path.parent == root)
        if target and (not targeted or mode == "hang"):
            targeted = True
            return instance.execute([sys.executable, __file__, "--helper", str(root),
                                     mode, *args[3:]], timeout=timeout, cleanup=True)
        return original(args, timeout)

    instance.cleanup_command = cleanup
    if mode == "hang":
        instance.cleanup_each = 1.5
        instance.cleanup_remaining = 3.2
    with mock.patch.object(seed.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4)):
        report = instance.run()
    report["calls"] = instance.calls
    (root / "report.json").write_text(json.dumps(report))


def helper(root, mode, name, device, inode):
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "ready").write_text(str(os.getpid()))
    until = time.monotonic() + 8
    while not (root / "release").exists() or mode == "hang":
        if time.monotonic() > until:
            raise TimeoutError("test barrier not released")
        time.sleep(0.01)
    seed.remove_owned(name, device, inode)


class SignalCleanupTests(unittest.TestCase):
    def run_scenario(self, mode):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, __file__, "--scenario", str(root), mode],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            until = time.monotonic() + 10
            while not (root / "ready").exists():
                self.assertIsNone(process.poll(), "scenario failed before barrier")
                self.assertLess(time.monotonic(), until, "cleanup child failed to start")
                time.sleep(0.01)
            if mode != "hang":
                # Delivery happens while the real deletion subprocess owns the
                # stage, not by throwing a synthetic exception at rmtree entry.
                os.kill(process.pid, signal.SIGTERM)
                os.kill(process.pid, signal.SIGALRM)
                time.sleep(0.03)
                os.kill(process.pid, signal.SIGTERM)
                (root / "release").touch()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)
        return root, json.loads((root / "report.json").read_text()), time.monotonic() - started

    def test_actual_repeated_signals_during_build_and_module_cleanup(self):
        for mode in ("build", "module"):
            with self.subTest(mode=mode):
                root, report, elapsed = self.run_scenario(mode)
                self.assertEqual(report["state"], "cancelled")
                self.assertEqual(report["cleanup"], "complete")
                self.assertFalse(list(root.rglob(".mo-seed-*")))
                self.assertFalse((root / "build" / seed.MARKER).exists())
                self.assertEqual((root / "build" / EXISTING).read_bytes(), b"local probe")
                self.assertEqual((root / "build" / MISSING).read_bytes(), b"imported build")
                self.assertLess(elapsed, 5)
                module_calls = [c for c in report["calls"] if "/go/pkg/mod" in " ".join(c)]
                self.assertEqual(bool(module_calls), mode == "module")
                if mode == "module":
                    self.assertTrue((root / "modules/example.test/m@v1/m.go").is_file())

    def test_unresponsive_cleanup_is_killed_with_bounded_shared_budget(self):
        root, report, elapsed = self.run_scenario("hang")
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["cleanup"], "incomplete")
        self.assertLess(elapsed, 5)
        self.assertTrue(report["leftover_resources"])
        self.assertFalse((root / "build" / seed.MARKER).exists())
        self.assertFalse(any("/go/pkg/mod" in " ".join(c) for c in report["calls"]))
        self.assertNotIn("leftover_pids", report)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((root / "ready").read_text()), 0)

    def test_creation_rename_and_marker_signal_windows(self):
        for point in ("mkdtemp", "mkstemp", "rename", "replace"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                with mock.patch.object(seed, "CHECKOUT", str(root)), \
                        mock.patch.object(seed, "MODULES", str(root / "modules")), \
                        mock.patch.object(seed.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4)):
                    instance = FakeSeeder(root)
                    original_cleanup = instance.cleanup_command
                    instance.cleanup_command = lambda args, timeout: (
                        None if args[0] == "docker" else original_cleanup(args, timeout))
                    owner = seed.tempfile if point in ("mkdtemp", "mkstemp") else seed.os
                    original = getattr(owner, point)

                    def transition(*args, **kwargs):
                        result = original(*args, **kwargs)
                        os.kill(os.getpid(), signal.SIGTERM)
                        return result

                    with mock.patch.object(owner, point, side_effect=transition):
                        report = instance.run()
                    self.assertEqual(report["state"], "cancelled")
                    self.assertEqual(report["cleanup"], "complete")
                    self.assertFalse(instance.owned)
                    self.assertFalse((instance.cache / seed.MARKER).exists())
                    self.assertFalse(list(root.rglob(".seed-record-*")))

    def test_changed_creation_identity_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            instance = seed.Seeder("race", "1")
            path = instance.temporary(root)
            preserved = root / "preserved"
            path.rename(preserved)
            path.mkdir()
            (path / "user-data").write_text("preserve")
            self.assertFalse(instance.cleanup_path(path))
            self.assertEqual((path / "user-data").read_text(), "preserve")
            self.assertIn(path, instance.owned)

    def check_commit_checkpoint_boundary(self, cancel_before):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(seed, "CHECKOUT", str(root)), \
                    mock.patch.object(seed, "MODULES", str(root / "modules")), \
                    mock.patch.object(seed.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4)):
                instance = FakeSeeder(root)
                original_cleanup = instance.cleanup_command
                instance.cleanup_command = lambda args, timeout: (
                    None if args[0] == "docker" else original_cleanup(args, timeout))
                checkpoint = instance.checkpoint
                observed = []

                def cancel_after_publication():
                    if (instance.cache / seed.MARKER).exists():
                        observed.append(True)
                        if not cancel_before:
                            checkpoint()
                        os.kill(os.getpid(), signal.SIGTERM)
                        if not cancel_before:
                            return
                    checkpoint()

                instance.checkpoint = cancel_after_publication
                report = instance.run()
                self.assertEqual(observed, [True])
                self.assertEqual(report["state"], "cancelled" if cancel_before else "seeded")
                self.assertEqual(report["cleanup"], "complete")
                self.assertEqual((instance.cache / seed.MARKER).exists(), not cancel_before)
                self.assertFalse(instance.owned)

    def test_final_post_publication_checkpoint_rolls_back_cancelled_marker(self):
        self.check_commit_checkpoint_boundary(cancel_before=True)

    def test_signal_after_successful_commit_checkpoint_retains_success_marker(self):
        self.check_commit_checkpoint_boundary(cancel_before=False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scenario":
        scenario(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "--helper":
        helper(Path(sys.argv[2]), *sys.argv[3:])
    else:
        unittest.main()
