import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("prepare_coverage_cgo.sh")


def _fake_make(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "make.log"
    make_path = bin_dir / "make"
    make_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
target="${1:-}"
printf '%s\\n' "${target}" >> "${MAKE_LOG}"
if [[ "${target}" == "${FAIL_TARGET:-}" ]]; then
  exit "${FAIL_CODE:-1}"
fi
if [[ "${target}" == "cgo" && "${CREATE_HEADERS:-0}" == "1" ]]; then
  mkdir -p thirdparties/install/include
  for header in xxhash.h roaring.h usearch.h; do
    if [[ "${header}" != "${OMIT_HEADER:-}" ]]; then
      touch "thirdparties/install/include/${header}"
    fi
  done
fi
"""
    )
    make_path.chmod(0o755)
    return bin_dir, log_path


def _run_helper(
    tmp_path: Path,
    bin_dir: Path,
    log_path: Path,
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "MAKE_LOG": str(log_path),
        }
    )
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_config_failure_stops_before_cgo(tmp_path):
    """A failed package-graph check must not start native compilation."""
    bin_dir, log_path = _fake_make(tmp_path)

    result = _run_helper(
        tmp_path,
        bin_dir,
        log_path,
        FAIL_TARGET="config",
        FAIL_CODE="23",
    )

    assert result.returncode == 23
    assert log_path.read_text().splitlines() == ["clean", "config"]


@pytest.mark.parametrize("missing_header", ["xxhash.h", "roaring.h", "usearch.h"])
def test_missing_required_headers_fails_preparation(tmp_path, missing_header):
    """A nominal make result must not admit an incomplete native install."""
    bin_dir, log_path = _fake_make(tmp_path)

    result = _run_helper(
        tmp_path,
        bin_dir,
        log_path,
        CREATE_HEADERS="1",
        OMIT_HEADER=missing_header,
    )

    assert result.returncode != 0
    assert f"thirdparties/install/include/{missing_header}" in result.stderr


def test_successful_preparation_builds_in_order(tmp_path):
    """A complete native build must admit the coverage test phase."""
    bin_dir, log_path = _fake_make(tmp_path)

    result = _run_helper(
        tmp_path,
        bin_dir,
        log_path,
        CREATE_HEADERS="1",
    )

    assert result.returncode == 0
    assert log_path.read_text().splitlines() == ["clean", "config", "cgo"]
