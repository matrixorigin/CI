#!/usr/bin/env bash

set -uo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  run_bvt_tenant_parallel.sh \
    --tester-dir PATH \
    --case-root PATH \
    --group 0|1 \
    --directories PATH \
    --output-dir PATH \
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
group=""
directories=""
output_dir=""
resource_dir=""
mysql_host="127.0.0.1"
mysql_port="6001"
mysql_user="dump"
mysql_password="111"
tenant_password="111"

while (( $# > 0 )); do
  case "$1" in
    --tester-dir|--case-root|--group|--directories|--output-dir|--resource-dir|--mysql-host|--mysql-port|--mysql-user|--mysql-password|--tenant-password)
      (( $# >= 2 )) || die "missing value for $1"
      option=$1
      value=$2
      shift 2
      case "${option}" in
        --tester-dir) tester_dir=${value} ;;
        --case-root) case_root=${value} ;;
        --group) group=${value} ;;
        --directories) directories=${value} ;;
        --output-dir) output_dir=${value} ;;
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
[[ -n "${group}" ]] || die "--group is required"
[[ -n "${directories}" ]] || die "--directories is required"
[[ -n "${output_dir}" ]] || die "--output-dir is required"
[[ "${group}" =~ ^[01]$ ]] || die "--group must be 0 or 1"
[[ "${mysql_port}" =~ ^[0-9]+$ ]] || die "--mysql-port must be numeric"
[[ "${tenant_password}" =~ ^[A-Za-z0-9._-]+$ ]] ||
  die "--tenant-password may contain only letters, digits, dot, underscore, and hyphen"

tester_dir=$(absolute_directory "${tester_dir}") ||
  die "tester directory does not exist"
case_root=$(absolute_directory "${case_root}") ||
  die "case root does not exist"
directories=$(absolute_file "${directories}") ||
  die "directory configuration does not exist"
[[ -f "${directories}" ]] ||
  die "directory configuration does not exist: ${directories}"

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

bvt_group=${group}
# shellcheck source=/dev/null
source "${directories}" || die "failed to load directory configuration"

validate_directory_name() {
  local name=$1
  [[ "${name}" =~ ^[A-Za-z0-9_]+$ ]] ||
    die "invalid case directory name: ${name}"
}

has_test_scripts() {
  local path=$1
  find "${path}" -type f \( -name '*.sql' -o -name '*.test' \) -print -quit |
    grep -q .
}

configured_directory() {
  local name=$1
  bvt_directory_in_array "${name}" \
    "${bvt_serial_before_all[@]}" \
    "${bvt_parallel_group_0_worker_0[@]}" \
    "${bvt_parallel_group_0_worker_1[@]}" \
    "${bvt_parallel_group_1_worker_0[@]}" \
    "${bvt_parallel_group_1_worker_1[@]}" \
    "${bvt_serial_after_all[@]}" \
    "${bvt_excluded[@]}"
}

filter_existing_directories() {
  local name
  for name in "$@"; do
    validate_directory_name "${name}"
    if [[ -d "${case_root}/${name}" ]] &&
       has_test_scripts "${case_root}/${name}"; then
      printf '%s\n' "${name}"
    fi
  done
}

serial_before_names=()
worker_0_names=()
worker_1_names=()
serial_after_names=()

while IFS= read -r directory_name; do
  [[ -n "${directory_name}" ]] &&
    serial_before_names+=("${directory_name}")
done < <(filter_existing_directories "${bvt_serial_before[@]}")

while IFS= read -r directory_name; do
  [[ -n "${directory_name}" ]] &&
    worker_0_names+=("${directory_name}")
done < <(filter_existing_directories "${bvt_worker_0[@]}")

while IFS= read -r directory_name; do
  [[ -n "${directory_name}" ]] &&
    worker_1_names+=("${directory_name}")
done < <(filter_existing_directories "${bvt_worker_1[@]}")

while IFS= read -r directory_name; do
  [[ -n "${directory_name}" ]] &&
    serial_after_names+=("${directory_name}")
done < <(filter_existing_directories "${bvt_serial_after[@]}")

inventory_file="${output_dir}/inventory.tsv"
printf 'directory\tgroup\tphase\treviewed\n' > "${inventory_file}"

for directory_name in \
  "${serial_before_names[@]}" \
  "${worker_0_names[@]}" \
  "${worker_1_names[@]}" \
  "${serial_after_names[@]}"; do
  case " ${serial_before_names[*]} " in
    *" ${directory_name} "*) phase="serial-before" ;;
    *)
      case " ${worker_0_names[*]} " in
        *" ${directory_name} "*) phase="worker-0" ;;
        *)
          case " ${worker_1_names[*]} " in
            *" ${directory_name} "*) phase="worker-1" ;;
            *) phase="serial-after" ;;
          esac
          ;;
      esac
      ;;
  esac
  printf '%s\t%s\t%s\ttrue\n' \
    "${directory_name}" "${group}" "${phase}" >> "${inventory_file}"
done

while IFS= read -r discovered_path; do
  directory_name=$(basename "${discovered_path}")
  validate_directory_name "${directory_name}"
  has_test_scripts "${discovered_path}" || continue
  if configured_directory "${directory_name}"; then
    continue
  fi
  if [[ "$(bvt_group_for_directory "${directory_name}")" == "${group}" ]]; then
    serial_after_names+=("${directory_name}")
    printf '%s\t%s\tserial-after\tfalse\n' \
      "${directory_name}" "${group}" >> "${inventory_file}"
    echo "Unreviewed BVT directory runs as sys-after: ${directory_name}"
  fi
done < <(
  find "${case_root}" -mindepth 1 -maxdepth 1 -type d -print |
    LC_ALL=C sort
)

write_include_file() {
  local path=$1
  shift
  local include_value=""
  local name
  for name in "$@"; do
    if [[ -n "${include_value}" ]]; then
      include_value+=","
    fi
    include_value+="${case_root}/${name}/"
  done
  printf '%s\n' "${include_value}" > "${path}"
}

serial_before_include="${output_dir}/serial-before.include"
worker_0_include="${output_dir}/worker-0.include"
worker_1_include="${output_dir}/worker-1.include"
serial_after_include="${output_dir}/serial-after.include"
write_include_file "${serial_before_include}" "${serial_before_names[@]}"
write_include_file "${worker_0_include}" "${worker_0_names[@]}"
write_include_file "${worker_1_include}" "${worker_1_names[@]}"
write_include_file "${serial_after_include}" "${serial_after_names[@]}"

selected_directory_count=$(
  (
    printf '%s\n' \
      "${serial_before_names[@]}" \
      "${worker_0_names[@]}" \
      "${worker_1_names[@]}" \
      "${serial_after_names[@]}"
  ) |
    sed '/^$/d' |
    wc -l |
    tr -d '[:space:]'
)
(( selected_directory_count > 0 )) ||
  die "BVT group ${group} has no test directories"

echo "BVT directory plan: group ${group}: ${selected_directory_count} directories; fixed 2 tenant workers"

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

created_accounts=()
worker_pids=()
worker_pid_count=0

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
    [[ -z "${quoted}" ]] || quoted+=","
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

redact_phase_configs() {
  python3 - "${output_dir}" <<'PY'
import re
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
secret_field = re.compile(r"^(\s*(?:password|syspass)\s*:\s*).*$", re.IGNORECASE)
for path in output_dir.glob("phases/*/tester/mo.yml"):
    lines = path.read_text().splitlines()
    redacted = [secret_field.sub(r'\1"***"', line) for line in lines]
    path.write_text("\n".join(redacted) + "\n")
PY
}

stop_worker_processes() {
  local index=0
  local pid
  while (( index < worker_pid_count )); do
    pid=${worker_pids[index]}
    if kill -0 "${pid}" 2>/dev/null; then
      kill -TERM -- "-${pid}" 2>/dev/null ||
        kill -TERM "${pid}" 2>/dev/null ||
        true
    fi
    ((index+=1))
  done
  index=0
  while (( index < worker_pid_count )); do
    wait "${worker_pids[index]}" 2>/dev/null || true
    ((index+=1))
  done
  worker_pids=()
  worker_pid_count=0
}

cleanup_on_exit() {
  local status=$?
  trap - EXIT INT TERM
  stop_worker_processes
  if (( ${#created_accounts[@]} > 0 )); then
    cleanup_accounts || status=1
  fi
  redact_phase_configs || status=1
  exit "${status}"
}

trap cleanup_on_exit EXIT
trap 'exit 130' INT TERM

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
    cd "${prepared_tester}" || exit 1
    ./run.sh "${tester_args[@]}" 2>&1 | tee "${prepared_log}"
  )
}

if [[ -s "${serial_before_include}" ]] &&
   [[ -n "$(<"${serial_before_include}")" ]]; then
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

active_workers=()
for worker_index in 0 1; do
  worker_include="${output_dir}/worker-${worker_index}.include"
  if [[ -n "$(<"${worker_include}")" ]]; then
    active_workers+=("${worker_index}")
  else
    record_summary \
      "parallel" "worker-${worker_index}" "skipped" "${worker_include}" ""
  fi
done

for worker_index in "${active_workers[@]}"; do
  account="bvtw_g${group}_w${worker_index}"
  if mysql_exec \
    "CREATE ACCOUNT IF NOT EXISTS \`${account}\` ADMIN_NAME 'admin' IDENTIFIED BY '${tenant_password}';"; then
    created_accounts+=("${account}")
  else
    echo "failed to create worker account ${account}" >&2
    record_summary "parallel" "account-setup" "failed" "" ""
    exit 1
  fi
done

launched_workers=()
if (( ${#active_workers[@]} > 0 )); then
  set -m
fi
for worker_index in "${active_workers[@]}"; do
  run_phase \
    "worker-${worker_index}" \
    "${output_dir}/worker-${worker_index}.include" \
    "bvtw_g${group}_w${worker_index}:admin" &
  worker_pids+=("$!")
  ((worker_pid_count+=1))
  launched_workers+=("${worker_index}")
done
set +m

final_status=0
pid_index=0
while (( pid_index < worker_pid_count )); do
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
worker_pids=()
worker_pid_count=0

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
elif [[ -n "$(<"${serial_after_include}")" ]]; then
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
