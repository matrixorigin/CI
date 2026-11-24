"""Contract tests for run_coverage_ut_phases.sh with a fake `go`."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("run_coverage_ut_phases.sh")

# The fake records one JSON line per invocation in GO_LOG, prints one go test
# JSON event per package, writes a profile row per package, and exits with the
# status named for the first package in FAIL_STATUS ("pkg=status,...").
FAKE_GO = r"""#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == test ]] || exit 99
profile=''
parallel=''
packages=()
args=("${@:2}")
for (( i=0; i<${#args[@]}; i++ )); do
  case "${args[i]}" in
    -p) parallel=${args[i+1]}; i=$((i + 1)) ;;
    -coverprofile=*) profile=${args[i]#-coverprofile=} ;;
    -*) ;;
    *) packages+=("${args[i]}") ;;
  esac
done
python3 - "$GO_LOG" "${parallel}" "${MO_TEST_CLUSTER_ADMISSION_POOL_SIZE:-}" "$*" "${packages[@]}" <<'PY'
import json, sys
log, parallel, pool, argv, *packages = sys.argv[1:]
with open(log, "a") as out:
    out.write(json.dumps({"parallel": parallel, "pool": pool, "argv": argv, "packages": packages}) + "\n")
PY
printf 'mode: set\n' > "${profile}"
printf 'warning: no packages being tested depend on matches for pattern x/unused\n' >&2
printf 'go: other stderr\n' >&2
for package in "${packages[@]}"; do
  printf '{"Action":"pass","Package":"%s"}\n' "${package}"
  printf '%s/a.go:1.1,2.2 1 1\n' "${package}" >> "${profile}"
done
status=0
IFS=',' read -r -a rules <<< "${FAIL_STATUS:-}"
for rule in "${rules[@]}"; do
  if [[ "${rule%%=*}" == "${packages[0]}" ]]; then status=${rule#*=}; fi
done
exit "${status}"
"""


class RunCoverageUTPhasesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "go"
        fake.write_text(FAKE_GO)
        fake.chmod(0o755)
        self.bin = bin_dir
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.log = self.tmp / "go.log"
        self.profile = self.tmp / "coverage.out"
        self.timing = self.tmp / "timing.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_phases(self, scope, embedded, fail_status=""):
        scope_file = self.tmp / "scope.txt"
        scope_file.write_text("".join(f"{p}\n" for p in scope) + "\n")
        embedded_file = self.tmp / "embedded.txt"
        embedded_file.write_text("".join(f"{p}\n" for p in embedded))
        env = os.environ.copy()
        env.update({"PATH": self.path, "GO_LOG": str(self.log), "FAIL_STATUS": fail_status})
        env.pop("MO_TEST_CLUSTER_ADMISSION_POOL_SIZE", None)
        return subprocess.run(
            [
                "bash", str(SCRIPT),
                "--profile", str(self.profile),
                "--phase-timing", str(self.timing),
                "--parallelism", "8",
                "--embedded-parallelism", "2",
                "--scope", str(scope_file),
                "--embedded", str(embedded_file),
                "--", "-json", "-covermode=set", "-coverpkg=a,b",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_embedded_packages_run_in_their_own_pass(self) -> None:
        result = self.run_phases(["a", "emb1", "b", "emb2"], ["emb1", "emb2", "not-in-scope"])
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([c["packages"] for c in calls], [["a", "b"], ["emb1", "emb2"]])
        self.assertEqual([c["parallel"] for c in calls], ["8", "2"])
        # Only the embedded pass gets the cluster admission pool.
        self.assertEqual([c["pool"] for c in calls], ["", "2"])
        for call in calls:
            self.assertIn("-covermode=set -coverpkg=a,b", call["argv"])
        # stdout is the JSON report: nothing but go test events.
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([e["Package"] for e in events], ["a", "b", "emb1", "emb2"])
        # The unimported -coverpkg warning is dropped; other stderr is kept.
        self.assertEqual(result.stderr, "go: other stderr\ngo: other stderr\n")
        profile = self.profile.read_text().splitlines()
        self.assertEqual(profile[0], "mode: set")
        self.assertEqual(profile.count("mode: set"), 1)
        self.assertEqual(profile[1:], [f"{p}/a.go:1.1,2.2 1 1" for p in ("a", "b", "emb1", "emb2")])
        self.assertFalse(Path(f"{self.profile}.ordinary").exists())
        self.assertFalse(Path(f"{self.profile}.embedded").exists())
        timing = self.timing.read_text()
        self.assertIn("phase=coverage_ut_ordinary status=0", timing)
        self.assertIn("phase=coverage_ut_embedded status=0", timing)

    def test_no_embedded_packages_is_one_pass(self) -> None:
        # A skipped pass must not contribute an old file from a prior run.
        Path(f"{self.profile}.embedded").write_text("mode: set\nstale/a.go:1.1,2.2 1 1\n")
        result = self.run_phases(["a", "b"], [])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c["packages"] for c in self.calls()], [["a", "b"]])
        self.assertIn("phase=coverage_ut_embedded status=skipped", self.timing.read_text())
        self.assertEqual(len(self.profile.read_text().splitlines()), 3)

    def test_embedded_only_ignores_stale_ordinary_profile(self) -> None:
        Path(f"{self.profile}.ordinary").write_text("mode: set\nstale/a.go:1.1,2.2 1 1\n")
        result = self.run_phases(["emb"], ["emb"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.profile.read_text().splitlines(), ["mode: set", "emb/a.go:1.1,2.2 1 1"])

    def test_merge_read_failure_preserves_profiles_without_publishing_partial_output(self) -> None:
        tail = self.bin / "tail"
        tail.write_text('#!/bin/sh\ncase "$*" in *".embedded") echo partial; exit 28;; esac\n'
                        'exec /usr/bin/tail "$@"\n')
        tail.chmod(0o755)
        result = self.run_phases(["a", "emb"], ["emb"])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.profile.exists())
        for name in ("ordinary", "embedded"):
            self.assertTrue(Path(f"{self.profile}.{name}").is_file(), name)
        self.assertEqual(list(self.tmp.glob("coverage.out.tmp.*")), [])

    def test_failed_publish_preserves_previous_output_and_inputs(self) -> None:
        self.profile.write_text("previous complete profile\n")
        mv = self.bin / "mv"
        mv.write_text("#!/bin/sh\nexit 28\n")
        mv.chmod(0o755)
        result = self.run_phases(["a", "emb"], ["emb"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.profile.read_text(), "previous complete profile\n")
        for name in ("ordinary", "embedded"):
            self.assertTrue(Path(f"{self.profile}.{name}").is_file(), name)
        self.assertEqual(list(self.tmp.glob("coverage.out.tmp.*")), [])

    def test_missing_empty_or_wrong_mode_profile_cannot_count_as_success(self) -> None:
        go = self.bin / "go"
        for sabotage in ('rm "$profile"', ': > "$profile"', 'echo "mode: count" > "$profile"'):
            with self.subTest(sabotage=sabotage):
                go.write_text(FAKE_GO.replace('exit "${status}"',
                              'if [[ "$profile" == *.embedded ]]; then ' + sabotage + '; fi\nexit "${status}"'))
                result = self.run_phases(["a", "emb"], ["emb"])
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.profile.exists())

    def test_ordinary_failure_still_runs_embedded_pass(self) -> None:
        result = self.run_phases(["a", "emb"], ["emb"], fail_status="a=1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([c["packages"] for c in self.calls()], [["a"], ["emb"]])
        self.assertEqual(len(self.profile.read_text().splitlines()), 3)

    def test_embedded_failure_is_reported(self) -> None:
        result = self.run_phases(["a", "emb"], ["emb"], fail_status="emb=1")
        self.assertEqual(result.returncode, 1)

    def test_signal_stops_before_embedded_pass(self) -> None:
        result = self.run_phases(["a", "emb"], ["emb"], fail_status="a=143")
        self.assertEqual(result.returncode, 143)
        self.assertEqual([c["packages"] for c in self.calls()], [["a"]])

    def test_invalid_arguments(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--profile", str(self.profile)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
