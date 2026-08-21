#!/usr/bin/env bash

set -euo pipefail

: "${COVERAGE_PROFILE:?COVERAGE_PROFILE must name the final profile}" \
  "${COVERAGE_REPORT:?COVERAGE_REPORT must name the final JSON report}" \
  "${COVER_PKGS:?COVER_PKGS must contain the complete coverpkg list}"

readonly heavy_package="${COVERAGE_HEAVY_PACKAGE:-github.com/matrixorigin/matrixone/pkg/sql/plan}"
readonly rest_parallelism="${COVERAGE_PARALLELISM:-6}"
readonly heavy_parallelism="${COVERAGE_HEAVY_PARALLELISM:-1}"

if [[ "${COVERAGE_PROFILE}" == "${COVERAGE_REPORT}" ]]; then
  echo "coverage profile and report must be different files" >&2
  exit 2
fi

packages=("$@")
rest_packages=()
heavy_count=0
for package in "${packages[@]}"; do
  if [[ "${package}" == "${heavy_package}" ]]; then
    heavy_count=$((heavy_count + 1))
  else
    rest_packages+=("${package}")
  fi
done

if (( heavy_count != 1 )); then
  echo "expected exactly one ${heavy_package} package, found ${heavy_count}" >&2
  exit 2
fi
if (( ${#rest_packages[@]} == 0 )); then
  echo "coverage package list has no non-${heavy_package} packages" >&2
  exit 2
fi

profile_dir=$(dirname -- "${COVERAGE_PROFILE}")
report_dir=$(dirname -- "${COVERAGE_REPORT}")
mkdir -p "${profile_dir}" "${report_dir}"
rm -f -- "${COVERAGE_PROFILE}"
: > "${COVERAGE_REPORT}"

# Keep the staging directory beside the final profile so the final rename is
# atomic even when a caller places the report outside RUNNER_TEMP.
phase_dir=$(mktemp -d "${profile_dir%/}/.matrixone-coverage.XXXXXX")
cleanup() {
  rm -rf -- "${phase_dir}"
}
trap cleanup EXIT

run_phase() {
  local name="$1"
  local parallelism="$2"
  local profile="$3"
  local report="$4"
  shift 4

  echo "coverage phase ${name}: $# package(s), -p ${parallelism}"
  local status
  if env \
    CGO_CFLAGS="${CGO_CFLAGS:-}" \
    CGO_LDFLAGS="${CGO_LDFLAGS:-}" \
    go test -mod=readonly -json -short -v -tags matrixone_test -p "${parallelism}" \
      -covermode=set -coverprofile="${profile}" -coverpkg="${COVER_PKGS}" \
      "$@" > "${report}" 2>&1; then
    status=0
  else
    status=$?
  fi
  local append_status
  if cat "${report}" >> "${COVERAGE_REPORT}"; then
    append_status=0
  else
    append_status=$?
  fi
  if (( status != 0 )); then
    return "${status}"
  fi
  return "${append_status}"
}

rest_profile="${phase_dir}/rest.out"
rest_report="${phase_dir}/rest.json"
plan_profile="${phase_dir}/plan.out"
plan_report="${phase_dir}/plan.json"
merged_profile="${phase_dir}/merged.out"

rest_status=0
run_phase "rest" "${rest_parallelism}" "${rest_profile}" "${rest_report}" "${rest_packages[@]}" || rest_status=$?
if (( rest_status != 0 )); then
  echo "coverage phase rest failed with status ${rest_status}" >&2
  exit "${rest_status}"
fi

plan_status=0
run_phase "plan" "${heavy_parallelism}" "${plan_profile}" "${plan_report}" "${heavy_package}" || plan_status=$?
if (( plan_status != 0 )); then
  echo "coverage phase plan failed with status ${plan_status}" >&2
  exit "${plan_status}"
fi

# Go emits one mode header per phase. Merge only block records, preserve
# hit/not-hit semantics with max(hit), and atomically publish the final file.
LC_ALL=C awk '
  $1 == "mode:" { next }
  NF < 3 { next }
  $1 ~ /pkg\/pb|pkg\/sql\/parsers\/goyacc|yaccpar/ { next }
  {
    key = $1 " " $2
    hit = ($3 + 0 > 0) ? 1 : 0
    if (!(key in coverage) || hit > coverage[key]) {
      coverage[key] = hit
    }
  }
  END {
    print "mode: set"
    for (key in coverage) {
      print key, coverage[key]
    }
  }
' "${rest_profile}" "${plan_profile}" > "${merged_profile}"

if [[ "$(wc -l < "${merged_profile}")" -le 1 ]]; then
  echo "coverage phases produced no profile blocks" >&2
  exit 1
fi
mv -f -- "${merged_profile}" "${COVERAGE_PROFILE}"
echo "UT coverage profile published: $(wc -c < "${COVERAGE_PROFILE}") bytes"
