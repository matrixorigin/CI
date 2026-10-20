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
        repository = workspace / "matrixone"
        cgo = repository / "cgo"
        thirdparty = repository / "thirdparties" / "install" / "lib"
        cgo.mkdir(parents=True)
        thirdparty.mkdir(parents=True)
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
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
        for workflow_name, workspace_root, variables, assignment_count, result_variable in [
            ("coverage-ut.yaml", repository, "thirdparties_install_dir|cgo_ldflags", 2, "cgo_ldflags"),
            (
                "robot.yaml",
                workspace,
                "CGO_LDFLAGS|THIRDPARTIES_DIR|THIRDPARTIES_INSTALL_DIR",
                4,
                "CGO_LDFLAGS",
            ),
        ]:
            with self.subTest(workflow=workflow_name):
                env["GITHUB_WORKSPACE"] = str(workspace_root)
                workflow = (Path(__file__).parents[1] / ".github/workflows" / workflow_name).read_text()
                assignments = re.findall(r"^ +(?:" + variables + r")=.*$", workflow, re.MULTILINE)
                self.assertEqual(len(assignments), assignment_count)
                flags = subprocess.check_output(
                    [
                        "bash",
                        "-eu",
                        "-c",
                        "\n".join(assignments) + '\nprintf "%s" "$' + result_variable + '"',
                    ],
                    env=env,
                    text=True,
                )
                for name, link_flags, missing_library in [
                    ("complete", flags, None),
                    ("missing_cgo_runtime_path", flags.replace(f"-Wl,-rpath,{cgo}", ""), "libmo.so"),
                    (
                        "missing_thirdparty_runtime_path",
                        flags.replace(f"-Wl,-rpath,{thirdparty}", ""),
                        "libusearch_c.so",
                    ),
                ]:
                    with self.subTest(workflow=workflow_name, runtime_path=name):
                        if missing_library is not None:
                            self.assertNotEqual(link_flags, flags, "negative control must remove its rpath")
                        binary = tmp_path / (workflow_name + "-" + name)
                        # The real Go cgo package supplies -lusearch_c even when
                        # the nightly workflow leaves it out of CGO_LDFLAGS.
                        subprocess.run(
                            ["cc", str(source), *shlex.split(link_flags), "-lusearch_c", "-o", str(binary)],
                            check=True,
                        )
                        result = subprocess.run(
                            [str(binary)], cwd=tmp_path, env=env, capture_output=True, text=True
                        )
                        if missing_library is None:
                            self.assertEqual(result.returncode, 0, result.stderr)
                        else:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn(missing_library, result.stderr)


if __name__ == "__main__":
    unittest.main()
