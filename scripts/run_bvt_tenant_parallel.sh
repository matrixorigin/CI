#!/usr/bin/env bash

set -uo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  run_bvt_tenant_parallel.sh \
    --tester-dir PATH \
    --case-root PATH \
    --group-runner PATH \
    --group 0|1 \
    --policy PATH \
    --planner PATH \
    --output-dir PATH \
    --workers 1..4 \
    [--resource-dir PATH] \
    [--mysql-host HOST] \
    [--mysql-port PORT] \
    [--mysql-user USER] \
    [--mysql-password PASSWORD] \
    [--tenant-password PASSWORD]
USAGE
}

die() {
  echo "tenant-parallel BVT: $*" >&2
  exit 2
}

absolute_directory() {
  local path=$1
  (cd "${path}" 2>/dev/null && pwd -P)
}

absolute_file() {
  local path=$1
  local directory
  directory=$(cd "$(dirname "${path}")" 2>/dev/null && pwd -P) || return 1
  printf '%s/%s\n' "${directory}" "$(basename "${path}")"
}

tester_dir=""
case_root=""
group_runner=""
group=""
policy=""
planner=""
output_dir=""
workers=""
resource_dir=""
mysql_host="127.0.0.1"
mysql_port="6001"
mysql_user="dump"
mysql_password="111"
tenant_password="111"

while (( $# > 0 )); do
  case "$1" in
    --tester-dir|--case-root|--group-runner|--group|--policy|--planner|--output-dir|--workers|--resource-dir|--mysql-host|--mysql-port|--mysql-user|--mysql-password|--tenant-password)
      (( $# >= 2 )) || die "missing value for $1"
      option=$1
      value=$2
      shift 2
      case "${option}" in
        --tester-dir) tester_dir=${value} ;;
        --case-root) case_root=${value} ;;
        --group-runner) group_runner=${value} ;;
        --group) group=${value} ;;
        --policy) policy=${value} ;;
        --planner) planner=${value} ;;
        --output-dir) output_dir=${value} ;;
        --workers) workers=${value} ;;
        --resource-dir) resource_dir=${value} ;;
        --mysql-host) mysql_host=${value} ;;
        --mysql-port) mysql_port=${value} ;;
        --mysql-user) mysql_user=${value} ;;
        --mysql-password) mysql_password=${value} ;;
        --tenant-password) tenant_password=${value} ;;
      esac
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "${tester_dir}" ]] || die "--tester-dir is required"
[[ -n "${case_root}" ]] || die "--case-root is required"
[[ -n "${group_runner}" ]] || die "--group-runner is required"
[[ -n "${group}" ]] || die "--group is required"
[[ -n "${policy}" ]] || die "--policy is required"
[[ -n "${planner}" ]] || die "--planner is required"
[[ -n "${output_dir}" ]] || die "--output-dir is required"
[[ -n "${workers}" ]] || die "--workers is required"
[[ "${group}" =~ ^[01]$ ]] || die "--group must be 0 or 1"
[[ "${workers}" =~ ^[1-4]$ ]] || die "--workers must be between 1 and 4"
[[ "${mysql_port}" =~ ^[0-9]+$ ]] || die "--mysql-port must be numeric"
[[ "${tenant_password}" =~ ^[A-Za-z0-9._-]+$ ]] ||
  die "--tenant-password may contain only letters, digits, dot, underscore, and hyphen"

tester_dir=$(absolute_directory "${tester_dir}") ||
  die "tester directory does not exist"
case_root=$(absolute_directory "${case_root}") ||
  die "case root does not exist"
group_runner=$(absolute_file "${group_runner}") ||
  die "group runner does not exist"
policy=$(absolute_file "${policy}") ||
  die "policy does not exist"
planner=$(absolute_file "${planner}") ||
  die "planner does not exist"
[[ -f "${group_runner}" ]] || die "group runner does not exist: ${group_runner}"
[[ -f "${policy}" ]] || die "policy does not exist: ${policy}"
[[ -f "${planner}" ]] || die "planner does not exist: ${planner}"

if [[ -z "${resource_dir}" && -d "$(dirname "${case_root}")/resources" ]]; then
  resource_dir="$(dirname "${case_root}")/resources"
fi
if [[ -n "${resource_dir}" ]]; then
  resource_dir=$(absolute_directory "${resource_dir}") ||
    die "resource directory does not exist"
fi

if [[ -e "${output_dir}" ]]; then
  [[ -d "${output_dir}" ]] || die "output path is not a directory"
  if find "${output_dir}" -mindepth 1 -print -quit | grep -q .; then
    die "output directory must be empty: ${output_dir}"
  fi
else
  mkdir -p "${output_dir}" || die "cannot create output directory"
fi
output_dir=$(absolute_directory "${output_dir}") ||
  die "cannot resolve output directory"

summary_file="${output_dir}/summary.tsv"
printf 'phase\tname\tstatus\tinclude_file\tlog_file\n' > "${summary_file}"

record_summary() {
  local phase=$1
  local name=$2
  local status=$3
  local include_file=$4
  local log_file=$5
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "${phase}" "${name}" "${status}" "${include_file}" "${log_file}" \
    >> "${summary_file}"
}

mysql_exec() {
  local sql=$1
  MYSQL_PWD="${mysql_password}" mysql \
    --host="${mysql_host}" \
    --port="${mysql_port}" \
    --user="${mysql_user}" \
    --batch \
    --skip-column-names \
    --execute "${sql}"
}

declare -a created_accounts=()

cleanup_accounts() {
  (( ${#created_accounts[@]} > 0 )) || return 0
  local failed=0
  local account
  for account in "${created_accounts[@]}"; do
    if ! mysql_exec "DROP ACCOUNT IF EXISTS \`${account}\`;"; then
      echo "failed to drop worker account ${account}" >&2
      failed=1
    fi
  done
  (( failed == 0 )) || return 1

  local quoted=""
  for account in "${created_accounts[@]}"; do
    if [[ -n "${quoted}" ]]; then
      quoted+=","
    fi
    quoted+="'${account}'"
  done
  local leak_count
  leak_count=$(mysql_exec \
    "SELECT count(*) FROM mo_catalog.mo_account WHERE account_name IN (${quoted});") ||
    return 1
  leak_count=$(printf '%s' "${leak_count}" | tr -d '[:space:]')
  [[ "${leak_count}" =~ ^[0-9]+$ ]] || {
    echo "invalid worker account cleanup result: ${leak_count}" >&2
    return 1
  }
  if (( leak_count != 0 )); then
    echo "${leak_count} worker account(s) remain after cleanup" >&2
    return 1
  fi
  created_accounts=()
}

cleanup_on_exit() {
  local status=$?
  trap - EXIT
  if (( ${#created_accounts[@]} > 0 )); then
    cleanup_accounts || status=1
  fi
  exit "${status}"
}

trap cleanup_on_exit EXIT
trap 'exit 130' INT TERM

capture_tester="${output_dir}/capture-tester"
mkdir -p "${capture_tester}"
cat > "${capture_tester}/run.sh" <<'CAPTURE'
#!/usr/bin/env bash
set -euo pipefail
include=""
while getopts ":p:m:t:r:i:e:s:ogfnch" opt; do
  if [[ "${opt}" == "i" ]]; then
    include=${OPTARG}
  fi
done
[[ -n "${include}" ]]
printf '%s\n' "${include}" | tr ',' '\n' > "${BVT_CAPTURE_FILE}"
CAPTURE
chmod +x "${capture_tester}/run.sh"

selected_files="${output_dir}/selected-files.txt"
capture_log="${output_dir}/group-capture.log"
group_command=(
  bash "${group_runner}"
  "${capture_tester}"
  "${case_root}"
  "${group}"
)
if [[ -n "${resource_dir}" ]]; then
  group_command+=("${resource_dir}")
fi
if ! BVT_CAPTURE_FILE="${selected_files}" \
  "${group_command[@]}" > "${capture_log}" 2>&1; then
  cat "${capture_log}" >&2
  die "failed to capture BVT group ${group}"
fi
[[ -s "${selected_files}" ]] || die "captured BVT group is empty"

if ! python3 "${planner}" \
  --case-root "${case_root}" \
  --selected-files "${selected_files}" \
  --policy "${policy}" \
  --workers "${workers}" \
  --output-dir "${output_dir}"; then
  die "failed to build BVT directory plan"
fi

prepare_phase() {
  local phase_name=$1
  local account_user=$2
  local phase_root="${output_dir}/phases/${phase_name}"
  local phase_tester="${phase_root}/tester"
  mkdir -p "${phase_tester}/log" "${phase_tester}/report"

  local required
  for required in run.sh run.yml mo.yml log4j.properties pprof.sh; do
    [[ -f "${tester_dir}/${required}" ]] ||
      die "mo-tester is missing ${required}"
    cp -a "${tester_dir}/${required}" "${phase_tester}/${required}"
  done
  [[ -d "${tester_dir}/lib" ]] || die "mo-tester is missing lib/"
  cp -a "${tester_dir}/lib" "${phase_tester}/lib"
  if [[ -f "${tester_dir}/kafka.yml" ]]; then
    cp -a "${tester_dir}/kafka.yml" "${phase_tester}/kafka.yml"
  fi
  chmod +x "${phase_tester}/run.sh" "${phase_tester}/pprof.sh"

  prepared_resource=""
  if [[ -n "${resource_dir}" ]]; then
    cp -a "${resource_dir}" "${phase_root}/resources"
    prepared_resource="${phase_root}/resources"
  fi

  if [[ -n "${account_user}" ]]; then
    python3 - "${phase_tester}/mo.yml" "${account_user}" "${tenant_password}" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
name = sys.argv[2]
password = sys.argv[3]
lines = path.read_text().splitlines()
name_count = 0
password_count = 0
for index, line in enumerate(lines):
    if line.startswith("  name:"):
        lines[index] = f'  name: "{name}"'
        name_count += 1
    elif line.startswith("  password:"):
        lines[index] = f'  password: "{password}"'
        password_count += 1
if name_count != 1 or password_count != 1:
    raise SystemExit(
        f"expected one user name and password in {path}; "
        f"found name={name_count}, password={password_count}"
    )
path.write_text("\n".join(lines) + "\n")
PY
  fi

  prepared_tester="${phase_tester}"
  prepared_log="${phase_root}/${phase_name}.log"
}

run_phase() {
  local phase_name=$1
  local include_file=$2
  local account_user=$3
  local include
  include=$(<"${include_file}")
  [[ -n "${include}" ]] || return 0

  prepare_phase "${phase_name}" "${account_user}"
  local -a tester_args=(-n -g -o -p "${case_root}" -i "${include}")
  if [[ -n "${prepared_resource}" ]]; then
    tester_args+=(-s "${prepared_resource}")
  fi
  (
    set -o pipefail
    cd "${prepared_tester}"
    ./run.sh "${tester_args[@]}" 2>&1 | tee "${prepared_log}"
  )
}

serial_before_include="${output_dir}/serial-before.include"
serial_after_include="${output_dir}/serial-after.include"

if [[ -s "${serial_before_include}" ]]; then
  if run_phase "serial-before" "${serial_before_include}" ""; then
    record_summary \
      "serial" "serial-before" "passed" \
      "${serial_before_include}" \
      "${output_dir}/phases/serial-before/serial-before.log"
  else
    record_summary \
      "serial" "serial-before" "failed" \
      "${serial_before_include}" \
      "${output_dir}/phases/serial-before/serial-before.log"
    exit 1
  fi
else
  record_summary "serial" "serial-before" "skipped" "${serial_before_include}" ""
fi

declare -a active_workers=()
worker_index=0
while (( worker_index < workers )); do
  worker_include="${output_dir}/worker-${worker_index}.include"
  if [[ -s "${worker_include}" ]]; then
    active_workers+=("${worker_index}")
  else
    record_summary "parallel" "worker-${worker_index}" "skipped" "${worker_include}" ""
  fi
  ((worker_index+=1))
done

account_setup_failed=0
for worker_index in "${active_workers[@]}"; do
  account="bvtw_g${group}_w${worker_index}"
  if mysql_exec \
    "CREATE ACCOUNT IF NOT EXISTS \`${account}\` ADMIN_NAME 'admin' IDENTIFIED BY '${tenant_password}';"; then
    created_accounts+=("${account}")
  else
    echo "failed to create worker account ${account}" >&2
    account_setup_failed=1
    break
  fi
done
if (( account_setup_failed != 0 )); then
  record_summary "parallel" "account-setup" "failed" "" ""
  exit 1
fi

declare -a worker_pids=()
declare -a launched_workers=()
for worker_index in "${active_workers[@]}"; do
  account="bvtw_g${group}_w${worker_index}:admin"
  run_phase \
    "worker-${worker_index}" \
    "${output_dir}/worker-${worker_index}.include" \
    "${account}" &
  worker_pids+=("$!")
  launched_workers+=("${worker_index}")
done

final_status=0
pid_index=0
while (( pid_index < ${#worker_pids[@]} )); do
  worker_index=${launched_workers[pid_index]}
  worker_include="${output_dir}/worker-${worker_index}.include"
  worker_log="${output_dir}/phases/worker-${worker_index}/worker-${worker_index}.log"
  if wait "${worker_pids[pid_index]}"; then
    record_summary \
      "parallel" "worker-${worker_index}" "passed" "${worker_include}" "${worker_log}"
  else
    record_summary \
      "parallel" "worker-${worker_index}" "failed" "${worker_include}" "${worker_log}"
    final_status=1
  fi
  ((pid_index+=1))
done

cleanup_ok=1
if (( ${#created_accounts[@]} > 0 )); then
  if cleanup_accounts; then
    record_summary "cleanup" "worker-accounts" "passed" "" ""
  else
    record_summary "cleanup" "worker-accounts" "failed" "" ""
    cleanup_ok=0
    final_status=1
  fi
fi

if (( cleanup_ok == 0 )); then
  record_summary "serial" "serial-after" "skipped" "${serial_after_include}" ""
elif ! mysql_exec "SELECT 1;" >/dev/null; then
  echo "MatrixOne is unreachable; serial-after cannot run" >&2
  record_summary "serial" "serial-after" "skipped" "${serial_after_include}" ""
  final_status=1
elif [[ -s "${serial_after_include}" ]]; then
  if run_phase "serial-after" "${serial_after_include}" ""; then
    record_summary \
      "serial" "serial-after" "passed" \
      "${serial_after_include}" \
      "${output_dir}/phases/serial-after/serial-after.log"
  else
    record_summary \
      "serial" "serial-after" "failed" \
      "${serial_after_include}" \
      "${output_dir}/phases/serial-after/serial-after.log"
    final_status=1
  fi
else
  record_summary "serial" "serial-after" "skipped" "${serial_after_include}" ""
fi

exit "${final_status}"
