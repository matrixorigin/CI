#!/usr/bin/env bash

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
orchestrator="${script_dir}/run_bvt_tenant_parallel.sh"
planner="${script_dir}/bvt_tenant_plan.py"
directory_config="${script_dir}/bvt_tenant_directories.sh"

fail() {
  echo "not ok - $*" >&2
  exit 1
}

assert_file() {
  [[ -f "$1" ]] || fail "expected file: $1"
}

assert_contains() {
  local path=$1
  local expected=$2
  grep -F -- "${expected}" "${path}" >/dev/null ||
    fail "expected '${expected}' in ${path}"
}

assert_not_contains() {
  local path=$1
  local unexpected=$2
  if grep -F -- "${unexpected}" "${path}" >/dev/null; then
    fail "did not expect '${unexpected}' in ${path}"
  fi
}

line_number() {
  local path=$1
  local pattern=$2
  grep -n -m1 -F -- "${pattern}" "${path}" | cut -d: -f1
}

assert_order() {
  local path=$1
  shift
  local previous=0
  local pattern current
  for pattern in "$@"; do
    current=$(line_number "${path}" "${pattern}")
    [[ -n "${current}" ]] || fail "missing event '${pattern}'"
    (( current > previous )) ||
      fail "event '${pattern}' was out of order in ${path}"
    previous=${current}
  done
}

wait_for_event() {
  local path=$1
  local expected=$2
  local attempt=0
  while (( attempt < 100 )); do
    if grep -F -- "${expected}" "${path}" >/dev/null; then
      return 0
    fi
    sleep 0.05
    ((attempt+=1))
  done
  return 1
}

array_contains() {
  local expected=$1
  shift
  local value
  for value in "$@"; do
    [[ "${value}" == "${expected}" ]] && return 0
  done
  return 1
}

test_directory_contract() {
  [[ -f "${directory_config}" ]] ||
    fail "expected directory contract: ${directory_config}"

  local bvt_group=0
  # shellcheck source=/dev/null
  source "${directory_config}"
  array_contains view "${bvt_worker_0[@]}" ||
    fail "group 0 worker 0 must contain view"
  array_contains auto_increment "${bvt_worker_1[@]}" ||
    fail "group 0 worker 1 must contain auto_increment"
  array_contains benchmark "${bvt_serial_after[@]}" ||
    fail "group 0 sys-after must contain benchmark"
  ! array_contains analyze "${bvt_worker_0[@]}" ||
    fail "analyze must not run in an ordinary tenant"
  ! array_contains benchmark "${bvt_worker_1[@]}" ||
    fail "benchmark must not run in an ordinary tenant"

  local -a all_selected=(
    "${bvt_serial_before[@]}"
    "${bvt_worker_0[@]}"
    "${bvt_worker_1[@]}"
    "${bvt_serial_after[@]}"
  )
  local duplicate
  duplicate=$(
    printf '%s\n' "${all_selected[@]}" |
      sort |
      uniq -d |
      head -n 1
  )
  [[ -z "${duplicate}" ]] ||
    fail "directory appears in more than one phase: ${duplicate}"

  bvt_group=1
  # shellcheck source=/dev/null
  source "${directory_config}"
  array_contains analyze "${bvt_serial_after[@]}" ||
    fail "group 1 sys-after must contain analyze"
  ! array_contains analyze "${bvt_worker_0[@]}" ||
    fail "analyze must not run in an ordinary tenant"
  ! array_contains analyze "${bvt_worker_1[@]}" ||
    fail "analyze must not run in an ordinary tenant"
}

setup_fixture() {
  fixture_root=$(mktemp -d)
  case_root="${fixture_root}/cases"
  tester_dir="${fixture_root}/mo-tester"
  resource_dir="${fixture_root}/resources"
  fake_bin="${fixture_root}/bin"
  event_log="${fixture_root}/events.log"
  mysql_log="${fixture_root}/mysql.log"
  worker_pid_file="${fixture_root}/worker.pid"
  output_dir="${fixture_root}/output"
  policy="${fixture_root}/policy.json"
  group_runner="${fixture_root}/run_bvt_group.sh"

  mkdir -p \
    "${case_root}/before" \
    "${case_root}/alpha" \
    "${case_root}/beta" \
    "${case_root}/after" \
    "${tester_dir}/lib" \
    "${tester_dir}/log" \
    "${tester_dir}/report" \
    "${resource_dir}/nested" \
    "${fake_bin}"
  printf 'select 1;\n' > "${case_root}/before/a.sql"
  printf 'select 2;\n' > "${case_root}/alpha/a.sql"
  printf 'select 3;\n' > "${case_root}/beta/a.sql"
  printf 'select 4;\n' > "${case_root}/after/a.test"
  printf 'resource\n' > "${resource_dir}/nested/payload.txt"
  : > "${tester_dir}/lib/fake.jar"
  : > "${event_log}"
  : > "${mysql_log}"

  printf '%s\n' \
    'jdbc:' \
    '  server:' \
    '  - addr: "127.0.0.1:6001"' \
    'user:' \
    '  name: "dump"' \
    '  password: "111"' \
    '  sysuser: "dump"' \
    '  syspass: "111"' > "${tester_dir}/mo.yml"
  printf '%s\n' 'method: "run"' > "${tester_dir}/run.yml"
  printf '%s\n' 'log4j.rootLogger=INFO' > "${tester_dir}/log4j.properties"
  printf '%s\n' 'bootstrap.servers: localhost:9092' > "${tester_dir}/kafka.yml"
  printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "${tester_dir}/pprof.sh"
  chmod +x "${tester_dir}/pprof.sh"

  cat > "${tester_dir}/run.sh" <<'TESTER'
#!/usr/bin/env bash
set -euo pipefail
phase=$(basename "$(dirname "$PWD")")
include=""
while getopts ":p:m:t:r:i:e:s:ogfnch" opt; do
  if [[ "${opt}" == "i" ]]; then
    include=${OPTARG}
  fi
done
user=$(sed -n 's/^  name: *"\([^"]*\)".*/\1/p' mo.yml)
printf '%s\n' "${phase}:${user}:${include}" >> "${FAKE_EVENT_LOG}"
mkdir -p report
printf '%s\n' "${phase}" > report/report.txt
if [[ "${FAKE_FAIL_PHASE:-}" == "${phase}" ]]; then
  exit 7
fi
if [[ "${FAKE_BLOCK_PHASE:-}" == "${phase}" ]]; then
  printf '%s\n' "${BASHPID}" > "${FAKE_WORKER_PID_FILE}"
  printf '%s\n' "${phase}-blocked" >> "${FAKE_EVENT_LOG}"
  trap 'printf "%s\n" "${phase}-stopped" >> "${FAKE_EVENT_LOG}"; exit 143' INT TERM
  while true; do
    sleep 1
  done
fi
TESTER
  chmod +x "${tester_dir}/run.sh"

  cat > "${group_runner}" <<'GROUP'
#!/usr/bin/env bash
set -euo pipefail
tester_dir=$1
case_root=$2
"${tester_dir}/run.sh" -n -g -o -p "${case_root}" -i \
  "${case_root}/before/a.sql,${case_root}/alpha/a.sql,${case_root}/beta/a.sql,${case_root}/after/a.test"
GROUP
  chmod +x "${group_runner}"

  cat > "${fake_bin}/mysql" <<'MYSQL'
#!/usr/bin/env bash
set -euo pipefail
sql="${*: -1}"
printf '%s\n' "${sql}" >> "${FAKE_MYSQL_LOG}"
lower=$(printf '%s' "${sql}" | tr '[:upper:]' '[:lower:]')
if [[ "${lower}" == *"create account"* ]]; then
  printf '%s\n' "create-account" >> "${FAKE_EVENT_LOG}"
elif [[ "${lower}" == *"drop account"* ]]; then
  if [[ -s "${FAKE_WORKER_PID_FILE}" ]] &&
     kill -0 "$(<"${FAKE_WORKER_PID_FILE}")" 2>/dev/null; then
    printf '%s\n' "drop-while-worker-alive" >> "${FAKE_EVENT_LOG}"
  fi
  printf '%s\n' "drop-account" >> "${FAKE_EVENT_LOG}"
elif [[ "${lower}" == *"select 1"* ]]; then
  printf '%s\n' "mysql-ready" >> "${FAKE_EVENT_LOG}"
fi
if [[ "${lower}" == *"count(*)"* ]]; then
  printf '%s\n' "${FAKE_LEAK_COUNT:-0}"
fi
if [[ -n "${FAKE_MYSQL_FAIL_MATCH:-}" && "${lower}" == *"${FAKE_MYSQL_FAIL_MATCH}"* ]]; then
  exit 9
fi
MYSQL
  chmod +x "${fake_bin}/mysql"

  cat > "${policy}" <<'JSON'
{
  "schema_version": 1,
  "directories": {
    "before": {"phase": "serial-before", "reason": "ordered"},
    "alpha": {"phase": "parallel", "reason": "safe"},
    "beta": {"phase": "parallel", "reason": "safe"},
    "after": {"phase": "serial-after", "reason": "global"}
  }
}
JSON
}

run_fixture() {
  local -a command=(
    env
    "PATH=${fake_bin}:${PATH}"
    "FAKE_EVENT_LOG=${event_log}"
    "FAKE_MYSQL_LOG=${mysql_log}"
    "FAKE_FAIL_PHASE=${FAKE_FAIL_PHASE:-}"
    "FAKE_BLOCK_PHASE=${FAKE_BLOCK_PHASE:-}"
    "FAKE_WORKER_PID_FILE=${worker_pid_file}"
    "FAKE_LEAK_COUNT=${FAKE_LEAK_COUNT:-0}"
    "FAKE_MYSQL_FAIL_MATCH=${FAKE_MYSQL_FAIL_MATCH:-}"
    bash "${orchestrator}"
    --tester-dir "${tester_dir}"
    --case-root "${case_root}"
    --group-runner "${group_runner}"
    --group 0
    --policy "${policy}"
    --planner "${planner}"
    --output-dir "${output_dir}"
    --workers 2
    --tenant-password "tenant-secret"
    --resource-dir "${resource_dir}"
  )
  if [[ "${RUN_FIXTURE_IN_PLACE:-0}" == "1" ]]; then
    exec "${command[@]}"
  fi
  "${command[@]}"
}

test_successful_phase_order_and_isolation() {
  setup_fixture

  run_fixture

  assert_file "${output_dir}/plan.json"
  assert_file "${output_dir}/inventory.tsv"
  assert_file "${output_dir}/summary.tsv"
  assert_file "${output_dir}/phases/worker-0/tester/report/report.txt"
  assert_file "${output_dir}/phases/worker-1/tester/report/report.txt"
  assert_file "${output_dir}/phases/worker-0/resources/nested/payload.txt"
  assert_contains "${output_dir}/phases/worker-0/tester/mo.yml" \
    'name: "bvtw_g0_w0:admin"'
  assert_contains "${output_dir}/phases/worker-1/tester/mo.yml" \
    'name: "bvtw_g0_w1:admin"'
  assert_not_contains "${output_dir}/phases/worker-0/tester/mo.yml" \
    'password: "111"'
  assert_not_contains "${output_dir}/phases/worker-0/tester/mo.yml" \
    'password: "tenant-secret"'
  assert_contains "${output_dir}/phases/worker-0/tester/mo.yml" \
    'password: "***"'
  assert_not_contains "${output_dir}/worker-0.include" ".sql"
  assert_not_contains "${output_dir}/worker-1.include" ".test"
  assert_order "${event_log}" \
    "serial-before:dump:" \
    "create-account" \
    "worker-" \
    "drop-account" \
    "serial-after:dump:"
  assert_contains "${output_dir}/summary.tsv" $'parallel\tworker-0\tpassed'
  assert_contains "${output_dir}/summary.tsv" $'parallel\tworker-1\tpassed'
}

test_worker_failure_waits_for_sibling_and_runs_cleanup_and_after() {
  setup_fixture
  export FAKE_FAIL_PHASE=worker-0

  if run_fixture; then
    fail "worker failure should fail the orchestrator"
  fi

  assert_contains "${event_log}" "worker-0:bvtw_g0_w0:admin:"
  assert_contains "${event_log}" "worker-1:bvtw_g0_w1:admin:"
  assert_contains "${event_log}" "drop-account"
  assert_contains "${event_log}" "serial-after:dump:"
  assert_contains "${output_dir}/summary.tsv" $'parallel\tworker-0\tfailed'
  assert_contains "${output_dir}/summary.tsv" $'parallel\tworker-1\tpassed'
  unset FAKE_FAIL_PHASE
}

test_serial_before_failure_prevents_tenant_workers() {
  setup_fixture
  export FAKE_FAIL_PHASE=serial-before

  if run_fixture; then
    fail "serial-before failure should fail the orchestrator"
  fi

  assert_contains "${event_log}" "serial-before:dump:"
  assert_not_contains "${event_log}" "create-account"
  assert_not_contains "${event_log}" "worker-0:"
  assert_not_contains "${event_log}" "worker-1:"
  unset FAKE_FAIL_PHASE
}

test_empty_parallel_phase_skips_accounts() {
  setup_fixture
  cat > "${policy}" <<'JSON'
{
  "schema_version": 1,
  "directories": {
    "before": {"phase": "serial-before", "reason": "ordered"},
    "alpha": {"phase": "serial-after", "reason": "global"},
    "beta": {"phase": "serial-after", "reason": "global"},
    "after": {"phase": "serial-after", "reason": "global"}
  }
}
JSON

  run_fixture

  assert_not_contains "${event_log}" "create-account"
  assert_not_contains "${event_log}" "worker-0:"
  assert_not_contains "${event_log}" "worker-1:"
  assert_contains "${event_log}" "serial-after:dump:"
}

test_leaked_account_fails_before_serial_after() {
  setup_fixture
  export FAKE_LEAK_COUNT=1

  if run_fixture; then
    fail "leaked account should fail the orchestrator"
  fi

  assert_contains "${event_log}" "drop-account"
  assert_not_contains "${event_log}" "serial-after:dump:"
  unset FAKE_LEAK_COUNT
}

test_signal_stops_workers_before_cleanup() {
  setup_fixture
  export FAKE_BLOCK_PHASE=worker-0

  RUN_FIXTURE_IN_PLACE=1 run_fixture &
  local orchestrator_pid=$!
  if ! wait_for_event "${event_log}" "worker-0-blocked"; then
    kill -TERM "${orchestrator_pid}" 2>/dev/null || true
    wait "${orchestrator_pid}" 2>/dev/null || true
    fail "worker did not enter blocking phase"
  fi

  kill -TERM "${orchestrator_pid}"
  local status=0
  wait "${orchestrator_pid}" || status=$?

  local worker_pid
  worker_pid=$(<"${worker_pid_file}")
  local worker_was_alive=0
  if kill -0 "${worker_pid}" 2>/dev/null; then
    worker_was_alive=1
    kill -TERM "${worker_pid}" 2>/dev/null || true
  fi

  unset FAKE_BLOCK_PHASE
  (( status == 130 )) || fail "expected signal exit 130, got ${status}"
  (( worker_was_alive == 0 )) || fail "worker remained alive after orchestrator exit"
  assert_not_contains "${event_log}" "drop-while-worker-alive"
  assert_order "${event_log}" \
    "worker-0-blocked" \
    "drop-account"
}

test_account_creation_failure_prevents_workers() {
  setup_fixture
  export FAKE_MYSQL_FAIL_MATCH="create account"

  if run_fixture; then
    fail "account creation failure should fail the orchestrator"
  fi

  unset FAKE_MYSQL_FAIL_MATCH
  assert_contains "${output_dir}/summary.tsv" \
    $'parallel\taccount-setup\tfailed'
  assert_not_contains "${event_log}" "worker-0:"
  assert_not_contains "${event_log}" "worker-1:"
  assert_not_contains "${event_log}" "serial-after:dump:"
}

test_unreachable_matrixone_skips_serial_after() {
  setup_fixture
  export FAKE_MYSQL_FAIL_MATCH="select 1;"

  if run_fixture; then
    fail "MatrixOne readiness failure should fail the orchestrator"
  fi

  unset FAKE_MYSQL_FAIL_MATCH
  assert_contains "${event_log}" "drop-account"
  assert_not_contains "${event_log}" "serial-after:dump:"
  assert_contains "${output_dir}/summary.tsv" \
    $'serial\tserial-after\tskipped'
}

test_directory_contract
echo "ok - static directory contract"
test_successful_phase_order_and_isolation
echo "ok - successful phase order and isolation"
test_worker_failure_waits_for_sibling_and_runs_cleanup_and_after
echo "ok - worker failure aggregation and cleanup"
test_serial_before_failure_prevents_tenant_workers
echo "ok - serial-before failure barrier"
test_empty_parallel_phase_skips_accounts
echo "ok - empty parallel phase"
test_leaked_account_fails_before_serial_after
echo "ok - leaked account detection"
test_signal_stops_workers_before_cleanup
echo "ok - signal stops workers before cleanup"
test_account_creation_failure_prevents_workers
echo "ok - account creation failure barrier"
test_unreachable_matrixone_skips_serial_after
echo "ok - unreachable MatrixOne skips serial-after"
