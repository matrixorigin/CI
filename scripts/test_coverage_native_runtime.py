import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


class CoverageNativeRuntimeTest(unittest.TestCase):
    def test_coverage_native_runtime_paths(self):
        """Coverage executables must load both native locations without ambient LD paths."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        workspace = tmp_path / "workspace"
        cgo = workspace / "cgo"
        thirdparty = workspace / "thirdparties" / "install" / "lib"
        cgo.mkdir(parents=True)
        thirdparty.mkdir(parents=True)
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
        env["GITHUB_WORKSPACE"] = str(workspace)
        workflow = (Path(__file__).parents[1] / ".github/workflows/coverage-ut.yaml").read_text()
        assignments = re.findall(
            r"^          (?:thirdparties_install_dir|cgo_ldflags)=.*$", workflow, re.MULTILINE
        )
        assert len(assignments) == 2
        flags = subprocess.check_output(
            ["bash", "-eu", "-c", "\n".join(assignments) + '\nprintf "%s" "$cgo_ldflags"'],
            env=env,
            text=True,
        )
        for name, directory, value in [("mo", cgo, 19), ("usearch_c", thirdparty, 23)]:
            source = tmp_path / f"{name}.c"
            source.write_text(f"int {name}_probe(void) {{ return {value}; }}\n")
            subprocess.run(
                ["cc", "-fPIC", "-shared", str(source), "-o", str(directory / f"lib{name}.so")],
                check=True,
            )
        source = tmp_path / "main.c"
        source.write_text(
            "int mo_probe(void); int usearch_c_probe(void);\n"
            "int main(void) { return mo_probe() + usearch_c_probe() == 42 ? 0 : 1; }\n"
        )
        for name, link_flags in [
            ("complete", flags),
            ("missing_cgo_runtime_path", flags.replace(f"-Wl,-rpath,{cgo} ", "")),
        ]:
            binary = tmp_path / name
            subprocess.run(["cc", str(source), *shlex.split(link_flags), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], cwd=tmp_path, env=env, capture_output=True, text=True)
            if name == "complete":
                assert result.returncode == 0, result.stderr
            else:
                assert result.returncode != 0
                assert "libmo.so" in result.stderr


if __name__ == "__main__":
    unittest.main()
