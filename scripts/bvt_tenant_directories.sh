#!/usr/bin/env bash

# Top-level MatrixOne BVT case directories are the scheduling unit. This file is
# sourced by run_bvt_tenant_parallel.sh after bvt_group has been validated.

if ! [[ "${bvt_group:-}" =~ ^[01]$ ]]; then
  echo "bvt_tenant_directories.sh: bvt_group must be 0 or 1" >&2
  return 2 2>/dev/null || exit 2
fi

bvt_group_0_directories=(
  array auto_increment benchmark dataXtest ddl disttae fake_pk function
  git4data hint join keyword load_data mo_cloud optimizer pg_cast plugin
  prepare procedure query_result sample save_query_result sequence snapshot
  sql_inject stage system system_variable temporary tenant tenxcloud_xx
  time_window union util vector view
)

bvt_group_1_directories=(
  analyze charset_collation comment cte database distinct dml dtype expression
  feature_limit foreign_key fulltext geo iceberg log metadata operator
  pessimistic_transaction pitr plan_cache publication_subscription qexec
  recursive_cte replace_statement result_count security set sql_source_type
  statement_query_type subquery table task udf window zz_accesscontrol
  zz_statement_query_type
)

bvt_serial_before_all=(
  log
  result_count
  sql_source_type
  statement_query_type
  zz_statement_query_type
)

bvt_parallel_group_0_worker_0=(
  view
)

bvt_parallel_group_0_worker_1=(
  auto_increment
  sequence
  procedure
  keyword
  sample
  pg_cast
  plugin
  time_window
  union
  fake_pk
  dataXtest
)

bvt_parallel_group_1_worker_0=(
  dtype
  expression
  comment
  recursive_cte
  qexec
  replace_statement
)

bvt_parallel_group_1_worker_1=(
  window
  fulltext
  operator
  geo
  charset_collation
  distinct
  udf
  cte
  plan_cache
)

bvt_serial_after_all=(
  analyze
  array
  benchmark
  database
  ddl
  disttae
  dml
  feature_limit
  foreign_key
  function
  git4data
  hint
  iceberg
  join
  load_data
  metadata
  mo_cloud
  optimizer
  pessimistic_transaction
  pitr
  prepare
  publication_subscription
  query_result
  save_query_result
  security
  set
  snapshot
  sql_inject
  stage
  subquery
  system
  system_variable
  table
  task
  temporary
  tenant
  tenxcloud_xx
  util
  vector
  zz_accesscontrol
)

bvt_excluded=(
  optimistic
)

bvt_directory_in_array() {
  local expected=$1
  shift
  local value
  for value in "$@"; do
    if [[ "${value}" == "${expected}" ]]; then
      return 0
    fi
  done
  return 1
}

bvt_group_for_directory() {
  local name=$1
  if bvt_directory_in_array "${name}" "${bvt_group_0_directories[@]}"; then
    printf '0\n'
  elif bvt_directory_in_array "${name}" "${bvt_group_1_directories[@]}"; then
    printf '1\n'
  else
    printf '%s' "${name}" |
      cksum |
      awk '{ print $1 % 2 }'
  fi
}

bvt_serial_before=()
bvt_serial_after=()
bvt_worker_0=()
bvt_worker_1=()

for bvt_directory_name in "${bvt_serial_before_all[@]}"; do
  if [[ "$(bvt_group_for_directory "${bvt_directory_name}")" == "${bvt_group}" ]]; then
    bvt_serial_before+=("${bvt_directory_name}")
  fi
done

for bvt_directory_name in "${bvt_serial_after_all[@]}"; do
  if [[ "$(bvt_group_for_directory "${bvt_directory_name}")" == "${bvt_group}" ]]; then
    bvt_serial_after+=("${bvt_directory_name}")
  fi
done

if [[ "${bvt_group}" == "0" ]]; then
  bvt_worker_0=("${bvt_parallel_group_0_worker_0[@]}")
  bvt_worker_1=("${bvt_parallel_group_0_worker_1[@]}")
else
  bvt_worker_0=("${bvt_parallel_group_1_worker_0[@]}")
  bvt_worker_1=("${bvt_parallel_group_1_worker_1[@]}")
fi

unset bvt_directory_name
