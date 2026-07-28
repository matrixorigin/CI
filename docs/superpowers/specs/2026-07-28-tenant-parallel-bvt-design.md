# Tenant-Parallel BVT Design

## Status

Approved direction: run one MatrixOne deployment on one GitHub Actions runner, then run multiple `mo-tester` processes against different MatrixOne tenants on that deployment.

This design targets the active PR coverage workflows:

- `.github/workflows/e2e-compose-parallel.yaml`
- `.github/workflows/e2e-standalone-parallel.yaml`

The first rollout keeps the existing complementary BVT group assignment across the two workflows. Each workflow parallelizes only its assigned group inside its own runner. Checkin Regression adoption is a later rollout after the PR workflow is stable.

The CI repository baseline for this design is commit:

`940ee29eea1ee59eed5d3bec69cae7586b9a1162`

## Goals

1. Reduce BVT wall-clock time without adding one runner per tenant.
2. Preserve the current union/disjoint guarantees of BVT groups 0 and 1.
3. Keep global-state and cross-account tests serial.
4. Produce an explainable per-directory classification and a merged report.
5. Make the new path opt-in until shadow validation proves it stable.
6. Leave the existing serial invocation available as an immediate fallback.

## Non-goals

- Enabling the dormant in-process parallel mode in `mo-tester`.
- Running one MatrixOne deployment per tenant.
- Moving or renaming MatrixOne BVT case directories.
- Converting every serial test into a parallel test in the first rollout.
- Changing Checkin Regression in the first rollout.

## Considered approaches

### CI-managed processes on one runner — selected

The CI workflow starts one MatrixOne deployment and creates two test tenants. It launches one `mo-tester` process per tenant, using isolated working and resource directories.

This keeps the implementation in `matrixorigin/CI`, does not depend on the disabled `mo-tester` parallel switch, and matches the requested same-cluster tenant isolation model.

### In-process scheduling inside `mo-tester`

This would provide tighter report integration but requires coordinated changes in `matrixorigin/mo-tester`. Its existing implementation is fixed to one extra tenant, lacks before/after barriers, and is disabled. It is not selected for the first rollout.

### One GitHub Actions runner per group

This is the current complementary-group model. It isolates failures well, but it creates separate MatrixOne deployments rather than using tenants on one deployment. It remains the outer deployment model but is not used for per-tenant parallelism.

## Current inventory

The baseline scan is anchored to MatrixOne commit:

`d17b5a1f8e83cee4999181b9509af6126517985c`

The full `test/distributed/cases` tree contains 1,155 `.sql` and `.test` scripts in 73 top-level directories. The current PR coverage selection excludes the `optimistic` directory, leaving 1,133 scripts in 72 top-level directories.

The conservative initial classification is:

| Phase | Directories | Scripts | Meaning |
|---|---:|---:|---|
| `serial-before` | 5 | 29 | Ordered observability producers and verifiers that must run before tenant-generated traffic |
| `parallel-candidate` | 29 | 215 | Every script in the directory passed the conservative scan; requires shadow validation |
| `serial-after` | 38 | 889 | At least one script in the directory has cross-account, cluster-global, explicit-user, recovery, or other high-risk behavior |

The directory is the smallest scheduling and policy unit. The planner never divides scripts from the same top-level directory between phases or workers.

This changes the expected optimization ceiling: only 215 of 1,133 scripts are initially parallel candidates. The simpler commands and safer maintenance are preferred over file-level parallel coverage; the actual wall-clock benefit must be established by shadow runs.

## Classification policy

The policy is stored as data in `scripts/bvt_tenant_policy.json`. It lists each top-level directory, its phase, and its reason. The planner emits `plan.json` and `inventory.tsv` with one record per directory.

The policy is explicit rather than reclassifying individual files at runtime. A newly added directory defaults to `serial-after` and is reported as unreviewed, so it is exercised but cannot enter a tenant worker without a policy review.

### Serial-before directories

The following directories run in their existing lexical order as sys before test tenants are created:

- `log`
- `result_count`
- `sql_source_type`
- `statement_query_type`
- `zz_statement_query_type`

They inspect statement, log, and result metadata. Running them after tenant workers would expose them to parallel test traffic.

### Parallel directories

The following directories are the initial parallel candidates:

- `analyze`
- `auto_increment`
- `benchmark`
- `charset_collation`
- `comment`
- `cte`
- `dataXtest`
- `distinct`
- `dtype`
- `expression`
- `fake_pk`
- `fulltext`
- `geo`
- `keyword`
- `operator`
- `pg_cast`
- `plan_cache`
- `plugin`
- `procedure`
- `qexec`
- `recursive_cte`
- `replace_statement`
- `sample`
- `sequence`
- `time_window`
- `udf`
- `union`
- `view`
- `window`

Each directory is assigned to exactly one tenant worker. `benchmark` remains one unit, which preserves the lexical DDL, load, query, and cleanup order below `benchmark/tpch`.

### Serial-after directories

The following directories run as sys after tenant workers:

- `array`
- `database`
- `ddl`
- `disttae`
- `dml`
- `feature_limit`
- `foreign_key`
- `function`
- `git4data`
- `hint`
- `iceberg`
- `join`
- `load_data`
- `metadata`
- `mo_cloud`
- `optimizer`
- `pessimistic_transaction`
- `pitr`
- `prepare`
- `publication_subscription`
- `query_result`
- `save_query_result`
- `security`
- `set`
- `snapshot`
- `sql_inject`
- `stage`
- `subquery`
- `system`
- `system_variable`
- `table`
- `task`
- `temporary`
- `tenant`
- `tenxcloud_xx`
- `util`
- `vector`
- `zz_accesscontrol`

These directories exercise cross-account state, account recovery, global feature configuration, background tasks, failpoints, external environments, or contain at least one script matching a global-state rule.

### Serial content rules

A directory is classified as serial-after during policy review when any script below it contains:

- `CREATE ACCOUNT`, `DROP ACCOUNT`, or `ALTER ACCOUNT`
- `RESTORE ACCOUNT`
- `SHOW ACCOUNTS`
- an `@session` directive with an explicit user or password
- `mo_ctl(...)`
- `mo_feature_registry_*`
- `SET GLOBAL`
- an `@system` command
- `system_metrics` or `mo_debug`
- an explicit `account_id = 0` assumption
- `current_account_id()` or `current_account_name()`
- `mo_catalog.mo_account`
- `KILL CONNECTION` or `KILL QUERY`

Matching ignores case. Comment matches are intentionally conservative in the first rollout. The scan explains why a directory is serial; it does not split safe-looking files out of that directory.

## Selection and planning

MatrixOne's `optools/run_bvt_group.sh` remains the source of truth for complementary groups 0 and 1.

The orchestrator captures its `-i` selection by invoking it with a temporary no-op `mo-tester/run.sh`. The captured paths are reduced to their top-level directories and passed to the planner. This avoids copying the group mapping into the CI repository.

The planner then:

1. validates every selected directory exists immediately below the case root;
2. verifies that every selected script maps to exactly one selected top-level directory;
3. looks up each directory in the explicit policy;
4. defaults an unknown directory to `serial-after` and reports it as unreviewed;
5. verifies the three phases are disjoint and their directory union equals the selected group;
6. assigns whole parallel directories to workers using longest-first balancing;
7. uses an optional `--timings <tsv>` input when available and aggregate directory file size as the deterministic fallback weight;
8. writes per-phase and per-worker directory include lists.

Every `mo-tester -i` argument is therefore a comma-separated list of directories, not hundreds of individual scripts.

## Runtime architecture

Each workflow job continues to consume one runner and start one MatrixOne deployment.

```text
runner
├── MatrixOne deployment
├── serial-before mo-tester (sys)
├── tenant worker 0 mo-tester
├── tenant worker 1 mo-tester
└── serial-after mo-tester (sys)
```

The default worker count is two and is configurable from one to four.

### Worker isolation

Each worker receives:

- a unique MatrixOne account, `bvtw_<index>`;
- a unique `mo-tester` working directory;
- a copied 12 MiB MatrixOne resource directory;
- its own `mo.yml`, logs, reports, and pprof directory;
- the shared case tree as read-only input.

The worker's default JDBC user is `<account>:admin`. The sys credentials remain available to `mo-tester` for its internal sync-commit connection, but classification prevents test scripts with explicit credentials or known global SQL from entering a tenant worker.

### Phase order

1. Capture the outer BVT group and build the plan.
2. Run serial-before as sys.
3. Create test tenants.
4. Run all tenant workers, each with a directory include list, and wait for every worker.
5. Collect reports and statuses.
6. Drop all test tenants.
7. Run serial-after as sys if MatrixOne is reachable.
8. Merge reports and return failure when any phase failed or was unexpectedly skipped.

Test tenants are removed before serial-after so account enumeration and recovery tests see the same account state as the serial baseline.

## Failure handling

- A serial-before failure prevents tenant workers from starting.
- Parallel worker assertion failures do not terminate sibling workers; all available reports are collected.
- If MatrixOne remains reachable, serial-after still runs after worker failures to maximize diagnostic coverage.
- If MatrixOne is unreachable, serial-after is recorded as skipped and the job fails.
- A shell trap attempts tenant cleanup on success, failure, timeout, and cancellation.
- Cleanup targets only the exact `bvtw_<index>` accounts created by the current process.
- The final cleanup check queries `mo_catalog.mo_account`; any leaked worker account fails the job.

Artifacts include:

- `plan.json`
- `inventory.tsv`
- per-phase and per-worker logs
- original `mo-tester` reports
- merged summary and timing table
- cleanup status

## Workflow integration and rollout

The reusable workflows gain:

- `tenant_parallel_enabled`, default `false`
- `tenant_parallel_workers`, default `2`

When disabled, the existing `run_bvt_group.sh` invocation is unchanged. When enabled, the workflow calls `scripts/run_bvt_tenant_parallel.sh`.

Validation uses a MatrixOne workflow pinned to the CI branch commit SHA. The serial baseline and tenant-parallel candidate run in separate jobs and separate MatrixOne deployments.

Rollout gates:

1. planner and shell tests pass;
2. a curated integration subset proves phase barriers, tenant isolation, failure reporting, and cleanup;
3. full shadow runs complete at least ten times with no new concurrency-caused failure;
4. selected-script union and result coverage match the serial baseline;
5. no worker account or resource output leaks;
6. no new MatrixOne crash, OOM, or restart;
7. the measured wall-clock improvement justifies enabling the feature; the previous 30% target is no longer assumed because directory-level classification leaves only 215 scripts parallel.

After the gates pass, callers enable tenant parallelism. The disabled path remains available for immediate fallback.

## Test strategy

`scripts/test_bvt_tenant_plan.py` covers:

- directory phase lookup and reason reporting;
- rejection of file-level policy entries;
- directory union/disjoint validation;
- unknown-directory serial fallback;
- preservation of all scripts below each selected directory;
- deterministic whole-directory worker balancing;
- malformed policy and path rejection.

Shell integration tests use fake `mysql` and `mo-tester` commands to verify phase order, all-worker wait behavior, exit aggregation, artifacts, and cleanup.

Repository validation runs:

- Python unit tests
- `shellcheck`
- `actionlint`
- planner `--dry-run` against a MatrixOne checkout

The cross-repository validation workflow then runs the curated subset and full shadow comparison.

## Security and trust

The workflows already execute the checked-out MatrixOne `optools/run_bvt_group.sh`. Capturing its selection does not broaden that trust boundary.

Secrets are not written to `plan.json`, inventory, or logs. Generated `mo.yml` files are included in failure artifacts only after password fields are redacted.

Account and filesystem cleanup use explicit generated paths and account names; no recursive cleanup accepts an empty or unresolved root.
