# Direct Tenant-Parallel BVT Design

## Decision

Keep one GitHub Actions runner and one MatrixOne deployment per existing BVT
group. Inside that runner, invoke `mo-tester/run.sh` directly:

1. run ordering-sensitive directories as `sys`;
2. create two ordinary accounts;
3. run two isolated `mo-tester` processes concurrently, one account per process;
4. wait for both processes and drop both accounts;
5. run the remaining directories as `sys`.

This replaces the Python planner and JSON policy. Directory lists are explicit
Bash arrays, and each `mo-tester -i` argument contains directory paths rather
than individual test files.

The new path remains opt-in through `tenant_parallel_enabled: false`. When it
is disabled, each workflow runs its existing `run_bvt_group.sh` command
unchanged.

## Why CI-level processes

`mo-tester` accepts one case root through `-p` and comma-separated include
substrings through `-i`. An include ending in `/` safely selects a whole
directory. Its dormant in-process parallel code is not suitable because it is
disabled, fixed to two threads, and uses a hard-coded account. Separate
processes also require separate `mo-tester` and resource directories because
each invocation cleans databases and writes fixed report paths.

## Directory rule

The immediate child directory of `test/distributed/cases` is the scheduling
unit. If any case in a directory needs account-management privileges,
cross-account state, cluster-global state, snapshots/PITR, publication state,
explicit credentials, system tables, failpoints, tasks, or external shared
state, the whole directory runs serially as `sys`.

Unknown future directories are assigned to the same complementary BVT group as
`run_bvt_group.sh` and run in `sys-after`. `optimistic` remains excluded,
matching the current group runner.

The classification was rescanned against MatrixOne main
`f6dab28046d70412cec132f0068840896852101c`:

| Phase | Directories | Scripts |
|---|---:|---:|
| sys-before | 5 | 29 |
| ordinary-tenant parallel | 27 | 175 |
| sys-after | 40 | 935 |
| excluded `optimistic` | 1 | 22 |

Runtime improvement must be measured in the opt-in trial because only about
15% of selected scripts are initially safe enough for ordinary tenants.

## Fixed classification

### Sys-before

```text
log
result_count
sql_source_type
statement_query_type
zz_statement_query_type
```

These directories inspect statement and log metadata, so they run before
parallel test traffic.

### Tenant-parallel

Group 0:

```text
worker 0: view
worker 1: auto_increment sequence procedure keyword sample pg_cast plugin
          time_window union fake_pk dataXtest
```

Group 1:

```text
worker 0: dtype expression comment recursive_cte qexec replace_statement
worker 1: window fulltext operator geo charset_collation distinct udf cte
          plan_cache
```

The split is fixed and balances current directory sizes without runtime
planning. Every directory stays on exactly one worker.

### Sys-after

```text
analyze array benchmark database ddl disttae dml feature_limit foreign_key
function git4data hint iceberg join load_data metadata mo_cloud optimizer
pessimistic_transaction pitr prepare publication_subscription query_result
save_query_result security set snapshot sql_inject stage subquery system
system_variable table task temporary tenant tenxcloud_xx util vector
zz_accesscontrol
```

`analyze` contains snapshot DDL and `benchmark` contains account/cluster
snapshot DDL, so both require `sys`.

## Runtime contract

- The orchestrator accepts a case root, tester directory, group number,
  directory configuration, output directory, and optional resource directory.
- It derives the selected group from the explicit directory arrays and applies
  the same `cksum % 2` fallback as `run_bvt_group.sh` for unknown directories.
- Every include value is an absolute directory path ending in `/`.
- Each phase gets an isolated tester copy, report directory, log directory,
  `mo.yml`, and optional resource copy.
- Worker users are `bvtw_g<group>_w<index>:admin`; account names are unique to
  the group and worker. Account creation and deletion use the sys connection.
- Both workers are always waited for. A worker assertion failure does not skip
  sibling completion, account cleanup, or reachable sys-after execution.
- On a signal, worker process groups are terminated and waited for before
  account deletion.
- Generated `mo.yml` artifacts redact both `password` and `syspass`.
- The final exit status is nonzero when any required phase, worker, cleanup, or
  leak check fails.

## Trial and rollback

The first trial invokes the reusable workflows from a branch while setting:

```yaml
tenant_parallel_enabled: true
ci_ref: codex/tenant-parallel-bvt
```

The job still uses one runner. Its artifacts contain phase logs, reports,
directory includes, and `summary.tsv`. Compare wall-clock time, failures, and
case coverage with an unchanged run from the same MatrixOne commit. Turning the
boolean off immediately restores the original test path.
