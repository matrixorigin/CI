"""Execute both Linux release staging blocks and inspect their real ZIPs."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = ("jieba.dict.utf8", "hmm_model.utf8", "user.dict.utf8", "idf.utf8", "stop_words.utf8")


class LinuxDictionaryPackagingTests(unittest.TestCase):
    def run_package(self, arch, mode, missing=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "etc/launch").mkdir(parents=True)
            (root / "etc/launch/launch.toml").write_text("config")
            (root / "mo-service").write_text("binary")
            (root / "thirdparties/install/lib").mkdir(parents=True)
            (root / "thirdparties/install/lib/libusearch_c.so").write_text("runtime")
            if mode != "absent":
                source = root / "pkg/monlp/tokenizer/dict"
                source.mkdir(parents=True)
                for name in FILES:
                    if name != missing:
                        (source / name).write_text("source:" + name)
                if mode == "staged":
                    shutil.copytree(source, root / "dict")
                    (root / "dict/jieba.dict.utf8").write_text("staged:jieba.dict.utf8")
            # Build ownership is separate: exercise the workflow's real staging
            # commands and zip, replacing only the expensive compile command.
            tools = root / "tools"
            tools.mkdir()
            (tools / "make").write_text("#!/bin/sh\nexit 0\n")
            (tools / "make").chmod(0o755)
            workflow = (ROOT / ".github/workflows/release.yaml").read_text()
            job = "linux-amd64-build-job" if arch == "x86_64" else "linux-arm64-build-job"
            section = workflow.split("  " + job + ":\n", 1)[1].split("\n  #", 1)[0]
            section = re.split(r"\n  [a-z][a-z0-9-]*:", section, maxsplit=1)[0]
            match = re.search(r"      - name: Build normal\n        run: \|\n((?:          [^\n]*\n|\n)+)", section)
            self.assertIsNotNone(match)
            script = textwrap.dedent(match[1]).replace("${{ steps.get_release.outputs.tag_name }}", "v4.2.8-hotfix-test")
            result = subprocess.run(["bash", "-e", "-c", script], cwd=root,
                                    env=dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"]),
                                    capture_output=True, text=True, timeout=15)
            name = "mo-v4.2.8-hotfix-test-linux-" + arch
            archive = root / (name + ".zip")
            if missing:
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(archive.exists())
                return
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with zipfile.ZipFile(archive) as payload:
                self.assertEqual(payload.read(name + "/mo-service"), b"binary")
                self.assertEqual(payload.read(name + "/etc/launch/launch.toml"), b"config")
                self.assertEqual(payload.read(name + "/lib/libusearch_c.so"), b"runtime")
                dictionaries = {p for p in payload.namelist() if p.startswith(name + "/dict/") and not p.endswith("/")}
                if mode == "absent":
                    self.assertEqual(dictionaries, set())
                else:
                    self.assertEqual(dictionaries, {name + "/dict/" + f for f in FILES})
                    for file in FILES:
                        expected = ("staged:" if mode == "staged" and file == "jieba.dict.utf8" else "source:") + file
                        self.assertEqual(payload.read(name + "/dict/" + file).decode(), expected)

    def test_complete_runtime_payload_and_older_release_compatibility(self):
        for arch in ("x86_64", "arm64"):
            for mode in ("staged", "source", "absent"):
                with self.subTest(arch=arch, mode=mode):
                    self.run_package(arch, mode)

    def test_partial_dictionary_cannot_be_uploaded(self):
        for arch in ("x86_64", "arm64"):
            for missing in FILES:
                with self.subTest(arch=arch, missing=missing):
                    self.run_package(arch, "source", missing=missing)


if __name__ == "__main__":
    unittest.main()
