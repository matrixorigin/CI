import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("coverage_cs_build_tools.sh")
TOOLS = ("gcc", "g++", "make", "pkg-config", "cmake", "python3")


class CoverageCSBuildToolsTest(unittest.TestCase):
    def setup_fixture(self, root, *, missing=(), updates=0, install=True):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        bash = shutil.which("bash")
        for utility in ("ln", "mktemp"):
            (bin_dir / utility).symlink_to(shutil.which(utility))
        stub = root / "stub"
        stub.write_text(f"#!{bash}\nexit 0\n")
        stub.chmod(0o755)
        for tool in (*TOOLS, "sleep"):
            if tool not in missing:
                (bin_dir / tool).symlink_to(stub)
        sudo = bin_dir / "sudo"
        sudo.write_text(f"#!{bash}\n" + r"""
printf '%s\n' "$*" >> "$RUNNER_TEMP/apt_calls"
case " $* " in
  *" update "*)
    if (( UPDATE_FAILURES != 0 )); then
      read -r remaining < "$RUNNER_TEMP/remaining"
      if (( remaining > 0 )); then
        printf '%s\n' "$((remaining-1))" > "$RUNNER_TEMP/remaining"
        exit 100
      fi
    fi
    ;;
  *" install "*)
    if [[ "$CREATE_TOOLS" == yes ]]; then
      for tool in gcc g++ make pkg-config cmake python3; do
        if ! command -v "$tool" > /dev/null; then
          ln -s "$RUNNER_TEMP/stub" "$RUNNER_TEMP/bin/$tool"
        fi
      done
    fi
    ;;
  *) exit 98 ;;
esac
""")
        sudo.chmod(0o755)
        (root / "remaining").write_text(f"{updates}\n")
        env = dict(os.environ, PATH=str(bin_dir), RUNNER_TEMP=str(root),
                   GITHUB_PATH=str(root / "github_path"), UPDATE_FAILURES=str(updates),
                   CREATE_TOOLS="yes" if install else "no")
        return subprocess.run([bash, str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_no_alias_does_not_trigger_apt_when_build_tools_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.setup_fixture(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "apt_calls").exists())
            alias = Path((root / "github_path").read_text().strip()) / "python"
            self.assertEqual(alias.readlink(), root / "bin/python3")

    def test_retries_transient_index_failure_and_installs_only_missing_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.setup_fixture(root, missing=("gcc", "g++", "make", "cmake"), updates=1)
            self.assertEqual(result.returncode, 0, result.stderr)
            first, retry, install = (root / "apt_calls").read_text().splitlines()
            self.assertEqual(first, retry)
            self.assertIn("APT::Update::Error-Mode=any update", retry)
            self.assertTrue(install.endswith(" install -y --no-install-recommends build-essential cmake"), install)

    def test_exhausted_index_retries_never_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.setup_fixture(root, missing=("make",), updates=3)
            calls = (root / "apt_calls").read_text().splitlines()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(call.endswith(" update") for call in calls))
            self.assertFalse((root / "github_path").exists())

    def test_install_without_required_command_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.setup_fixture(Path(tmp), missing=("pkg-config",), install=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing build command after setup: pkg-config", result.stdout)
