import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import package_release as release


class PackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tag = "v4.2.6"
        self.name = "mo-v4.2.6-darwin-arm64"
        self.files = {
            "mo-service": "binary",
            "cgo/libmo.dylib": "libmo",
            "thirdparties/install/lib/libusearch_c.dylib": "usearch",
            "thirdparties/install/lib/onnxruntime_arm64.dylib": "onnx",
            "thirdparties/install/lib/libmo.a": "static input",
            "etc/launch/launch.toml": "config",
            "etc/launch-with-python-udf-server/launch.toml": "python config",
            "pkg/udf/pythonservice/server.py": "python",
            "pkg/monlp/tokenizer/dict/jieba.dict.utf8": "dictionary",
            "optools/sign/gon.json": json.dumps({
                "source": ["./mo-service"], "bundle_id": "com.matrixorigin.matrixone",
                "sign": {"application_identity": "test identity"},
                "apple_id": {"username": "test@example.invalid"},
                "zip": {"output_path": "old-binary-only.zip"},
            }),
        }
        for name, content in self.files.items():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        self.calls = []
        self.configs = []
        self.failure = None

    def fake_run(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        if args[0] == "gon":
            config = json.loads(Path(args[1]).read_text())
            self.configs.append(config)
            phase = "notarize" if "notarize" in config else "sign"
            if phase == "notarize":
                with zipfile.ZipFile(config["notarize"][0]["path"]) as archive:
                    self.assertIn(self.name + "/lib/libmo.dylib", archive.namelist())
                    self.assertIn(self.name + "/dict/jieba.dict.utf8", archive.namelist())
            if self.failure == phase:
                raise subprocess.CalledProcessError(1, args)
        elif args[0] == "ditto":
            if self.failure == "archive":
                raise subprocess.CalledProcessError(1, args)
            if "-c" in args:
                package, archive = map(Path, args[-2:])
                shutil.make_archive(str(archive.with_suffix("")), "zip", package.parent, package.name)
            else:
                with zipfile.ZipFile(args[-2]) as archive:
                    archive.extractall(args[-1])
        elif args[-1] == "--version":
            self.assertFalse(any(key.startswith("DYLD_") for key in kwargs["env"]))
            self.assertNotIn("LD_LIBRARY_PATH", kwargs["env"])
            self.assertNotIn("AC_PASSWORD", kwargs["env"])
            self.assertNotIn("AC_PROVIDER", kwargs["env"])
            self.assertEqual(Path(args[0]).parent, kwargs["cwd"])
            if self.failure == "smoke":
                raise subprocess.CalledProcessError(1, args)
        elif args[0] == "otool":
            binary = Path(args[-1])
            if binary.name == "mo-service":
                return "mo-service:\n\t@rpath/libmo.dylib (compatibility version 0.0.0)\n"
            return f"{binary}:\n\t@rpath/{binary.name} (compatibility version 0.0.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        elif args[0] == "codesign":
            phase = "notarization-verify" if "--check-notarization" in args else "verify"
            if self.failure == phase:
                raise subprocess.CalledProcessError(1, args)
        elif args[0] == "spctl":
            # App assessment rejects even valid notarized standalone CLI tools.
            raise subprocess.CalledProcessError(3, args, stderr="the code is valid but does not seem to be an app")
        return ""

    def invoke(self):
        with patch.object(release, "run", side_effect=self.fake_run):
            return release.package_release(self.root, self.tag, "gon")

    def assert_clean(self):
        self.assertEqual(list(self.root.glob(".macos-release-*")), [])
        for name, content in self.files.items():
            self.assertEqual((self.root / name).read_text(), content)

    def test_complete_archive_is_signed_then_notarized_without_repacking(self):
        with patch.dict(os.environ, {"AC_PASSWORD": "never-serialize-me", "DYLD_LIBRARY_PATH": "/developer/lib"}):
            archive = self.invoke()
        self.assertTrue(archive.is_file())
        self.assertEqual(len(self.configs), 2)
        signed = {Path(file).name for file in self.configs[0]["source"]}
        self.assertEqual(signed, {"mo-service", "libmo.dylib", "libusearch_c.dylib", "onnxruntime_arm64.dylib"})
        self.assertNotIn("zip", self.configs[0])
        self.assertNotIn("never-serialize-me", json.dumps(self.configs))
        self.assertEqual(sum(args[0] == "ditto" and "-c" in args for args in self.calls), 1)
        self.assertEqual(sum(args[-1] == "--version" for args in self.calls), 2)
        self.assertFalse(any(args[0] == "spctl" for args in self.calls))
        # The final gate must check the extracted CLI, after notarization and
        # before its smoke test. Merely deleting the app-only check is not enough.
        notarize_index = max(i for i, args in enumerate(self.calls) if args[0] == "gon")
        extract_index = next(i for i, args in enumerate(self.calls) if args[0] == "ditto" and "-x" in args)
        extracted_binary = str(Path(self.calls[extract_index][-1]) / self.name / "mo-service")
        ticket_checks = [(i, args) for i, args in enumerate(self.calls)
                         if args[0] == "codesign" and "--check-notarization" in args]
        self.assertEqual(len(ticket_checks), 1)
        verify_index, verify_args = ticket_checks[0]
        self.assertEqual(verify_args, ["codesign", "--verify", "--strict", "--verbose=2",
                                       "-R=notarized", "--check-notarization", extracted_binary])
        self.assertLess(notarize_index, extract_index)
        self.assertLess(extract_index, verify_index)
        self.assertEqual(self.calls[verify_index + 1:], [[extracted_binary, "--version"]])
        with zipfile.ZipFile(archive) as zipped:
            self.assertNotIn(self.name + "/lib/libmo.a", zipped.namelist())
            self.assertIn(self.name + "/pkg/udf/pythonservice/server.py", zipped.namelist())
        self.assert_clean()

    def test_failures_never_publish_and_clean_staging(self):
        for phase in ("sign", "verify", "smoke", "archive", "notarize", "notarization-verify"):
            with self.subTest(phase=phase):
                self.calls.clear()
                self.configs.clear()
                self.failure = phase
                with self.assertRaises(subprocess.CalledProcessError) as failed:
                    self.invoke()
                if phase == "notarization-verify":
                    self.assertEqual(len(self.configs), 2)
                    self.assertIn("notarize", self.configs[-1])
                    self.assertIn("--check-notarization", failed.exception.cmd)
                    self.assertIn("extracted", Path(failed.exception.cmd[-1]).parts)
                    self.assertEqual(sum(args[-1] == "--version" for args in self.calls), 1)
                self.assertFalse((self.root / (self.name + ".zip")).exists())
                self.assert_clean()

    def test_missing_runtime_dependency_fails_before_signing(self):
        (self.root / "cgo/libmo.dylib").unlink()
        with self.assertRaisesRegex(ValueError, "Missing packaged dependency"):
            self.invoke()
        self.assertFalse(self.configs)
        self.assertEqual(list(self.root.glob(".macos-release-*")), [])

    def test_tag_cannot_escape_output_directory(self):
        self.tag = "../../escape"
        with self.assertRaisesRegex(ValueError, "Invalid release tag"):
            self.invoke()
        self.assertFalse(self.calls)

    def test_existing_archive_is_not_reused_or_overwritten(self):
        archive = self.root / (self.name + ".zip")
        archive.write_text("previous")
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertEqual(archive.read_text(), "previous")
        self.assertFalse(self.calls)

    def test_optional_payloads_are_not_required_for_old_releases(self):
        shutil.rmtree(self.root / "etc/launch-with-python-udf-server")
        shutil.rmtree(self.root / "pkg")
        package = self.root / "stage"
        release.stage(self.root, package)
        self.assertTrue((package / "lib/libmo.dylib").is_file())
        self.assertFalse((package / "dict").exists())

    def test_dependency_validation_rejects_host_or_escaping_paths(self):
        package = self.root / "stage"
        release.stage(self.root, package)
        for dependency in ("/opt/homebrew/lib/libfoo.dylib", "@rpath/../../libfoo.dylib"):
            with self.subTest(dependency=dependency):
                def output(args, **kwargs):
                    if args[0] != "otool":
                        return ""
                    identity = "\t@rpath/self.dylib (compatibility version 0)\n" if Path(args[-1]).suffix == ".dylib" else ""
                    return f"binary:\n{identity}\t{dependency} (compatibility version 0)\n"
                with patch.object(release, "run", side_effect=output):
                    with self.assertRaises(ValueError):
                        release.verify_dependencies(package)

    def test_subprocess_timeout_kills_the_owned_process_group(self):
        with patch.object(release.subprocess, "Popen") as popen, patch.object(release.os, "killpg") as kill:
            process = popen.return_value.__enter__.return_value
            process.pid = 12345
            process.communicate.side_effect = [subprocess.TimeoutExpired("gon", 1), ("", None)]
            with self.assertRaises(subprocess.TimeoutExpired):
                release.run(["gon"], timeout=1)
            kill.assert_called_once_with(12345, signal.SIGKILL)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_cancellation_kills_child_group_and_remains_cancelled(self):
        with patch.object(release.subprocess, "Popen") as popen, patch.object(release.os, "killpg") as kill:
            process = popen.return_value.__enter__.return_value
            process.pid = 12345
            process.communicate.side_effect = [SystemExit(143), ("", None)]
            with self.assertRaises(SystemExit) as cancelled:
                release.run(["gon"])
            self.assertEqual(cancelled.exception.code, 143)
            kill.assert_called_once_with(12345, signal.SIGKILL)

    def test_cancelled_packaging_cleans_staging_without_output(self):
        def cancelled(args, **kwargs):
            if args[0] == "gon":
                raise SystemExit(143)
            return self.fake_run(args, **kwargs)
        with patch.object(release, "run", side_effect=cancelled):
            with self.assertRaises(SystemExit):
                release.package_release(self.root, self.tag, "gon")
        self.assertFalse((self.root / (self.name + ".zip")).exists())
        self.assert_clean()


@unittest.skipUnless(sys.platform == "darwin" and platform.machine() == "arm64", "native Apple Silicon fixture")
class NativePackagingTest(unittest.TestCase):
    def test_real_arm64_dylib_loads_after_archive_extraction_without_loader_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "source"
            (workspace / "cgo").mkdir(parents=True)
            (workspace / "etc/launch").mkdir(parents=True)
            (workspace / "thirdparties/install/lib").mkdir(parents=True)
            (root / "usearch.c").write_text("int base(void) { return 40; }\n")
            (root / "library.c").write_text("int base(void); int answer(void) { return base() + 2; }\n")
            (root / "main.c").write_text("int answer(void); int main(void) { return answer() == 42 ? 0 : 1; }\n")
            release.run(["clang", "-dynamiclib", root / "usearch.c", "-Wl,-install_name,@rpath/libusearch_c.dylib", "-o", workspace / "thirdparties/install/lib/libusearch_c.dylib"])
            release.run(["clang", "-dynamiclib", root / "library.c", "-L" + str(workspace / "thirdparties/install/lib"), "-lusearch_c", "-Wl,-install_name,@rpath/libmo.dylib", "-o", workspace / "cgo/libmo.dylib"])
            release.run(["clang", root / "main.c", "-L" + str(workspace / "cgo"), "-lmo", "-Wl,-rpath,@executable_path/lib", "-o", workspace / "mo-service"])
            package = root / "mo-native"
            release.stage(workspace, package)
            release.verify_dependencies(package)
            for binary in release.code_files(package):
                release.run(["codesign", "--force", "--sign", "-", binary])
                release.run(["codesign", "--verify", "--strict", binary])
            archive = root / "native.zip"
            release.run(["ditto", "-c", "-k", "--keepParent", package, archive])
            shutil.rmtree(workspace)
            shutil.rmtree(package)
            extracted = root / "extract"
            release.run(["ditto", "-x", "-k", archive, extracted])
            release.verify_dependencies(extracted / "mo-native")
            release.smoke(extracted / "mo-native")
            (extracted / "mo-native/lib/libmo.dylib").unlink()
            with self.assertRaisesRegex(ValueError, "Missing packaged dependency"):
                release.verify_dependencies(extracted / "mo-native")


if __name__ == "__main__":
    unittest.main()
