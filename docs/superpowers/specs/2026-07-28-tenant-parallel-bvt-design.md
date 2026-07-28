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
4. Produce an explainable per-script classification and a merged report.
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

The full `test/distributed/cases` tree contains 1,155 `.sql` and `.test` scripts. The current PR coverage selection excludes paths containing `optimistic`, leaving 1,133 scripts.

The conservative initial classification is:

| Phase | Scripts | Meaning |
|---|---:|---|
| `serial-before` | 29 | Ordered observability producers and verifiers that must run before tenant-generated traffic |
| `parallel-candidate` | 749 | No known global-state rule matched; requires shadow validation |
| `serial-after` | 355 | Cross-account, cluster-global, explicit-user, recovery, or other high-risk behavior |

The 355 serial-after scripts are selected by exclusive first-match reason:

| Reason | Scripts |
|---|---:|
| Serial suite override | 256 |
| `mo_ctl` use | 41 |
| Account DDL | 37 |
| Explicit session user/password | 11 |
| `SET GLOBAL` | 6 |
| Sys account ID assumption | 1 |
| `mo_catalog.mo_account` access | 1 |
| Account restore | 1 |
| `system_metrics` access | 1 |

These numbers describe static candidates, not a claim that all 749 scripts are already safe. Shadow runs can only move scripts from parallel to serial unless a reviewed policy change explicitly relaxes a rule.

## Classification policy

The policy is stored as data in `scripts/bvt_tenant_policy.json`. The planner emits `plan.json` and `inventory.tsv`, including the selected phase and every matching reason for every script.

### Serial-before suites

The following suites run in their existing lexical order as sys before test tenants are created:

- `log`
- `result_count`
- `sql_source_type`
- `statement_query_type`
- `zz_statement_query_type`

They inspect statement, log, and result metadata. Running them after tenant workers would expose them to parallel test traffic.

### Serial-after suite overrides

The following suites are serial even when an individual file does not match a content rule:

- `feature_limit`
- `git4data`
- `mo_cloud`
- `pitr`
- `publication_subscription`
- `snapshot`
- `sql_inject`
- `system`
- `system_variable`
- `task`
- `tenant`
- `tenxcloud_xx`
- `zz_accesscontrol`

These suites exercise cross-account state, account recovery, global feature configuration, background tasks, failpoints, or external environments.

### Serial content rules

A script is serial-after when it contains any of:

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

Matching ignores case. Comment matches are intentionally conservative in the first rollout.

### Explicit overrides

The policy supports exact-path overrides with a mandatory reason. Precedence is:

1. non-overridable hard blockers;
2. exact serial overrides;
3. reviewed exact parallel overrides;
4. suite and remaining content rules.

- `serial-before` and `serial-after` overrides handle hidden ordering or isolation dependencies found in shadow runs.
- A `parallel` override requires review.
- Account DDL, account restore, explicit credentials, and system commands are hard blockers and cannot be overridden to parallel.

### Affinity groups

Most parallel scripts are independent scheduling units. Multi-file workloads that depend on lexical execution order are assigned as one unit. The initial affinity rule keeps `benchmark/tpch/**` on one worker and preserves path order.

Additional affinity groups are added only with an identified producer/consumer dependency.

## Selection and planning

MatrixOne's `optools/run_bvt_group.sh` remains the source of truth for complementary groups 0 and 1.

The orchestrator captures its `-i` selection by invoking it with a temporary no-op `mo-tester/run.sh`. The captured paths are passed to the planner. This avoids copying the group mapping into the CI repository.

The planner then:

1. validates every selected path exists below the case root;
2. classifies every path;
3. verifies the three phases are disjoint and their union equals the selected group;
4. creates affinity units;
5. assigns parallel units to workers using longest-first balancing;
6. uses an optional `--timings <tsv>` input when available and file size as the deterministic fallback weight;
7. writes per-phase and per-worker include lists.

Unknown newly added suites are classified by content rules. They are never omitted. The plan records that they used the fallback policy.

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
4. Run all tenant workers and wait for every worker.
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
7. median BVT wall-clock time improves by at least 30%.

After the gates pass, callers enable tenant parallelism. The disabled path remains available for immediate fallback.

## Test strategy

`scripts/test_bvt_tenant_plan.py` covers:

- phase classification and reason reporting;
- exact override precedence;
- union/disjoint validation;
- unknown-suite handling;
- affinity preservation;
- deterministic worker balancing;
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
