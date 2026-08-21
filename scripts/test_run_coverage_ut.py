import json
import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_coverage_ut.sh")
HEAVY = "github.com/matrixorigin/matrixone/pkg/sql/plan"


def _fake_go(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "go.log"
    fake_go = bin_dir / "go"
    fake_go.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
packages=()
parallel="unknown"
previous=""
for arg in "$@"; do
  if [[ "$previous" == "-p" ]]; then
    parallel="$arg"
  fi
  previous="$arg"
  if [[ "$arg" == github.com/* ]]; then
    packages+=("$arg")
  fi
done
last_index=$((${#packages[@]} - 1))
package="${packages[$last_index]}"
printf '%s|%s\\n' "$parallel" "${packages[*]}" >> "${FAKE_GO_LOG}"
if [[ "${FAKE_GO_FAIL_PACKAGE:-}" == "$package" ]]; then
  printf '{"Action":"fail","Package":"%s"}\\n' "${packages[*]}"
  exit "${FAKE_GO_FAIL_CODE:-17}"
fi
profile=""
for arg in "$@"; do
  case "$arg" in
    -coverprofile=*) profile="${arg#-coverprofile=}" ;;
  esac
done
mkdir -p "$(dirname "$profile")"
printf '%s\\n' 'mode: set' > "$profile"
if [[ "$package" == "${FAKE_GO_HEAVY_PACKAGE}" ]]; then
  printf '%s\\n' 'pkg/shared.go:1.1,1.2 1 1' 'pkg/plan.go:1.1,1.2 1 0' 'pkg/pb/ignored.go:1.1,1.2 1 1' >> "$profile"
else
  printf '%s\\n' 'pkg/shared.go:1.1,1.2 1 0' 'pkg/rest.go:1.1,1.2 1 1' >> "$profile"
fi
printf '{"Action":"pass","Package":"%s"}\\n' "${packages[*]}"
"""
    )
    fake_go.chmod(0o755)
    return bin_dir, log_path


def _run(
    tmp_path: Path, packages: list[str], **extra_env: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    bin_dir, log_path = _fake_go(tmp_path)
    profile = tmp_path / "out" / "coverage.out"
    report = tmp_path / "out" / "report.json"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FAKE_GO_LOG": str(log_path),
            "FAKE_GO_HEAVY_PACKAGE": HEAVY,
            "COVERAGE_PROFILE": str(profile),
            "COVERAGE_REPORT": str(report),
            "COVER_PKGS": ",".join(packages),
        }
    )
    env.update(extra_env)
    result = subprocess.run(
        ["bash", str(SCRIPT), *packages],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, profile, report, log_path


def test_rest_finishes_before_plan_and_excludes_only_exact_package(tmp_path):
    packages = [
        "github.com/matrixorigin/matrixone/pkg/sql/plan_extra",
        HEAVY,
        "github.com/matrixorigin/matrixone/pkg/sql/other",
    ]
    result, _, _, log_path = _run(tmp_path, packages)
    assert result.returncode == 0, result.stderr
    rows = [line.split("|", 1) for line in log_path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0] == ["6", f"{packages[0]} {packages[2]}"]
    assert rows[1] == ["1", HEAVY]
    assert "coverage phase rest" in result.stdout
    assert "coverage phase plan" in result.stdout


def test_profiles_merge_max_hit_and_single_header(tmp_path):
    result, profile, _, _ = _run(
        tmp_path, ["github.com/matrixorigin/matrixone/pkg/sql/rest", HEAVY]
    )
    assert result.returncode == 0, result.stderr
    lines = profile.read_text().splitlines()
    assert lines.count("mode: set") == 1
    assert "pkg/shared.go:1.1,1.2 1 1" in lines
    assert "pkg/rest.go:1.1,1.2 1 1" in lines
    assert "pkg/plan.go:1.1,1.2 1 0" in lines
    assert not any("pkg/pb/" in line for line in lines)


def test_rest_failure_skips_plan_and_keeps_diagnostic_without_final_profile(tmp_path):
    result, profile, report, log_path = _run(
        tmp_path,
        ["github.com/matrixorigin/matrixone/pkg/sql/rest", HEAVY],
        FAKE_GO_FAIL_PACKAGE="github.com/matrixorigin/matrixone/pkg/sql/rest",
        FAKE_GO_FAIL_CODE="23",
    )
    assert result.returncode == 23
    assert len(log_path.read_text().splitlines()) == 1
    assert not profile.exists()
    assert json.loads(report.read_text().splitlines()[0])["Action"] == "fail"


def test_plan_failure_keeps_both_reports_and_does_not_publish_stale_profile(tmp_path):
    profile = tmp_path / "out" / "coverage.out"
    profile.parent.mkdir()
    profile.write_text("mode: set\nstale.go:1.1,1.2 1 1\n")
    result, profile, report, log_path = _run(
        tmp_path,
        ["github.com/matrixorigin/matrixone/pkg/sql/rest", HEAVY],
        FAKE_GO_FAIL_PACKAGE=HEAVY,
        FAKE_GO_FAIL_CODE="29",
    )
    assert result.returncode == 29
    assert len(log_path.read_text().splitlines()) == 2
    assert not profile.exists()
    actions = [json.loads(line)["Action"] for line in report.read_text().splitlines()]
    assert actions == ["pass", "fail"]


def test_missing_heavy_package_is_rejected(tmp_path):
    result, profile, _, _ = _run(
        tmp_path, ["github.com/matrixorigin/matrixone/pkg/sql/rest"]
    )
    assert result.returncode == 2
    assert not profile.exists()
