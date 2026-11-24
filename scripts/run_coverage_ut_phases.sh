#!/usr/bin/env bash
# Run coverage UT as two sequential go test passes: every package that does
# not start an embedded MO cluster, then the embedded-cluster packages on
# their own with bounded parallelism. Running everything in one `go test -p N`
# let embedded clusters overlap with heavy packages and with the linking of
# other coverage binaries, so a cluster's CN could see the runner's free
# memory fall below its admission reserve and reject trivial statements
# (matrixorigin/matrixone#33077). optools/run_ut.sh already isolates these
# packages for the race UT; this gives coverage UT the same shape.
#
# stdout carries only `go test -json` events: the caller stores it as the JSON
# report. Diagnostics go to the phase-timing file.
#
# Usage:
#   run_coverage_ut_phases.sh --profile FILE --phase-timing FILE \
#     --parallelism N --embedded-parallelism M \
#     --scope FILE --embedded FILE -- GO_TEST_FLAGS...
#
# --scope lists every package to test, one per line. --embedded lists the
# embedded-cluster packages; an empty file runs a single pass. GO_TEST_FLAGS
# must not include -p, -coverprofile or package arguments.
set -uo pipefail

profile=''
phase_timing=''
parallelism=''
embedded_parallelism=''
scope_file=''
embedded_file=''
while (( $# > 0 )); do
  case "$1" in
    --profile) profile=$2; shift 2 ;;
    --phase-timing) phase_timing=$2; shift 2 ;;
    --parallelism) parallelism=$2; shift 2 ;;
    --embedded-parallelism) embedded_parallelism=$2; shift 2 ;;
    --scope) scope_file=$2; shift 2 ;;
    --embedded) embedded_file=$2; shift 2 ;;
    --) shift; break ;;
    *) echo "run_coverage_ut_phases.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "${profile}" || -z "${phase_timing}" || -z "${scope_file}" || -z "${embedded_file}" ]] ||
  ! [[ "${parallelism}" =~ ^[1-9][0-9]*$ && "${embedded_parallelism}" =~ ^[1-9][0-9]*$ ]] ||
  (( $# == 0 )); then
  echo "run_coverage_ut_phases.sh: invalid arguments" >&2
  exit 2
fi
go_test_flags=("$@")

mapfile -t scope < <(sed '/^[[:space:]]*$/d' "${scope_file}")
mapfile -t embedded_listed < <(sed '/^[[:space:]]*$/d' "${embedded_file}")
declare -A is_embedded=()
for package in ${embedded_listed[@]+"${embedded_listed[@]}"}; do
  is_embedded["${package}"]=1
done
ordinary=()
embedded=()
for package in ${scope[@]+"${scope[@]}"}; do
  if [[ -n "${is_embedded[${package}]:-}" ]]; then
    embedded+=("${package}")
  else
    ordinary+=("${package}")
  fi
done

log_phase() {
  echo "$*" >> "${phase_timing}"
}

# run_pass NAME PARALLELISM PROFILE PACKAGES... runs one go test pass and
# records its result. A pass with no packages is skipped.
run_pass() {
  local name=$1 pass_parallelism=$2 pass_profile=$3
  shift 3
  if (( $# == 0 )); then
    log_phase "phase=coverage_ut_${name} status=skipped packages=0"
    return 0
  fi
  local started=${SECONDS} status
  log_phase "phase=coverage_ut_${name} status=started packages=$# parallelism=${pass_parallelism}"
  rm -f "${pass_profile}" || return 1
  # -coverpkg names every package, so a pass warns once for each one that no
  # package in that pass imports. The single pass never did; drop the warning
  # so the report stays pure JSON, and pass any other stderr through.
  {
    go test "${go_test_flags[@]}" -p "${pass_parallelism}" -coverprofile="${pass_profile}" "$@" 2>&1 1>&3 3>&- |
      grep --line-buffered -v '^warning: no packages being tested depend on matches for pattern ' >&2
    status=${PIPESTATUS[0]}
  } 3>&1
  log_phase "phase=coverage_ut_${name} status=${status} elapsed_seconds=$((SECONDS - started))"
  return "${status}"
}

ordinary_profile="${profile}.ordinary"
embedded_profile="${profile}.embedded"
run_pass ordinary "${parallelism}" "${ordinary_profile}" ${ordinary[@]+"${ordinary[@]}"}
ordinary_status=$?
# A signal means the caller is stopping the run; do not start another pass.
if (( ordinary_status > 128 )); then
  exit "${ordinary_status}"
fi
# The cluster admission pool lets this many processes hold a cluster at once;
# keep it equal to the pass parallelism so every running package can proceed.
MO_TEST_CLUSTER_ADMISSION_POOL_SIZE="${embedded_parallelism}" \
  run_pass embedded "${embedded_parallelism}" "${embedded_profile}" ${embedded[@]+"${embedded[@]}"}
embedded_status=$?

# Stage beside the destination so rename is atomic. Only consume profiles
# from passes we ran, and keep them for diagnosis if any merge step fails.
merge_profiles() (
  local staged part header
  (( $# > 0 )) || { echo "no coverage passes to merge" >&2; return 1; }
  [[ ! -d "${profile}" ]] || { echo "profile destination is a directory: ${profile}" >&2; return 1; }
  staged=$(mktemp "${profile}.tmp.XXXXXXXX") || return 1
  trap 'rm -f -- "${staged}"' EXIT
  printf 'mode: set\n' > "${staged}" || return 1
  for part in "$@"; do
    if ! IFS= read -r header < "${part}" || [[ "${header}" != 'mode: set' ]]; then
      echo "missing or invalid mode:set profile: ${part}" >&2
      return 1
    fi
    tail -n +2 "${part}" >> "${staged}" || return 1
  done
  mv -f -- "${staged}" "${profile}" || return 1
  trap - EXIT
  rm -f -- "$@"
)

parts=()
[[ -z "${ordinary[0]:-}" ]] || parts+=("${ordinary_profile}")
[[ -z "${embedded[0]:-}" ]] || parts+=("${embedded_profile}")
merge_profiles "${parts[@]}"
merge_status=$?
if (( merge_status != 0 )); then
  echo "coverage profile merge failed: ${profile}" >&2
fi

if (( ordinary_status != 0 )); then
  exit "${ordinary_status}"
fi
if (( embedded_status != 0 )); then
  exit "${embedded_status}"
fi
exit "${merge_status}"
