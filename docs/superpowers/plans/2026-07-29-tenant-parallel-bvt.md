# Direct Tenant-Parallel BVT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the directory-safe part of each PR BVT group through two direct,
concurrent `mo-tester` commands using ordinary tenants, with all privileged and
global cases serialized as `sys`.

**Architecture:** A sourced Bash file owns the complete top-level directory
classification and fixed two-worker split. A Bash orchestrator prepares
isolated tester/resource copies, invokes `run.sh -p CASE_ROOT -i DIRS` for each
phase, manages two accounts, and aggregates failures. The two reusable
workflows select this path behind an opt-in boolean and otherwise retain their
existing group runner.

**Tech Stack:** Bash, MySQL CLI, `mo-tester`, Python standard-library workflow
contract tests, GitHub Actions YAML.

## Global Constraints

- One workflow job uses exactly one runner and one MatrixOne deployment.
- The scheduling unit is one immediate child directory of
  `test/distributed/cases`; no test-file list is maintained.
- The parallel worker count is fixed at exactly `2`.
- Account-management, tenant, snapshot/PITR, publication, explicit-credential,
  cluster-global, task, failpoint, and system behavior runs as `sys`.
- Unknown directories run in `sys-after`; `optimistic` remains excluded.
- Include values are comma-separated absolute directory paths ending in `/`.
- Each concurrent invocation has isolated tester, report, log, and resource
  directories.
- Workers are waited for before accounts are dropped, including failure and
  signal paths.
- `tenant_parallel_enabled` defaults to `false`; the legacy command is
  unchanged when disabled.

---

### Task 1: Replace dynamic planning with a static directory contract

**Files:**
- Create: `scripts/bvt_tenant_directories.sh`
- Delete: `scripts/bvt_tenant_plan.py`
- Delete: `scripts/bvt_tenant_policy.json`
- Delete: `scripts/test_bvt_tenant_plan.py`
- Modify: `scripts/test_run_bvt_tenant_parallel.sh`

**Interfaces:**
- Consumes: global variable `bvt_group` set to `0` or `1`, plus a case root
  supplied to helper functions by the orchestrator.
- Produces arrays `bvt_serial_before`, `bvt_worker_0`, `bvt_worker_1`,
  `bvt_serial_after`, and `bvt_excluded`.
- Produces function `bvt_group_for_directory NAME` that prints `0` or `1` using
  the explicit MatrixOne group lists and `cksum % 2` for unknown names.

- [ ] **Step 1: Write the failing directory-contract test**

Add a shell test that sources the configuration for groups 0 and 1 and asserts:

```bash
assert_array_contains bvt_worker_0 view
assert_array_contains bvt_worker_1 auto_increment
assert_array_contains bvt_serial_after analyze
assert_array_contains bvt_serial_after benchmark
assert_array_not_contains bvt_worker_0 analyze
assert_array_not_contains bvt_worker_1 benchmark
assert_all_unique_selected_directories
```

Also construct a case root with an unknown top-level directory and assert the
orchestrator writes it to `serial-after.include`, never a worker include.

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
bash scripts/test_run_bvt_tenant_parallel.sh
```

Expected: fail because `scripts/bvt_tenant_directories.sh` does not exist and
the old orchestrator still requires `--planner`, `--policy`, and `--workers`.

- [ ] **Step 3: Add the directory arrays and group fallback**

Define literal arrays:

```bash
bvt_serial_before_all=(
  log result_count sql_source_type statement_query_type zz_statement_query_type
)
bvt_parallel_group_0_worker_0=(view)
bvt_parallel_group_0_worker_1=(
  auto_increment sequence procedure keyword sample pg_cast plugin
  time_window union fake_pk dataXtest
)
bvt_parallel_group_1_worker_0=(
  dtype expression comment recursive_cte qexec replace_statement
)
bvt_parallel_group_1_worker_1=(
  window fulltext operator geo charset_collation distinct udf cte plan_cache
)
```

Add the complete 40-directory sys-after list, the two explicit
`run_bvt_group.sh` directory lists, and `optimistic`. Build phase arrays by
filtering every literal directory through `bvt_group_for_directory`.

- [ ] **Step 4: Run the directory-contract test to verify GREEN**

Run:

```bash
bash scripts/test_run_bvt_tenant_parallel.sh directory_contract
```

Expected: the directory contract scenario passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/bvt_tenant_directories.sh scripts/test_run_bvt_tenant_parallel.sh
git rm scripts/bvt_tenant_plan.py scripts/bvt_tenant_policy.json scripts/test_bvt_tenant_plan.py
git commit -m "refactor: define BVT tenant execution by directory"
```

---

### Task 2: Rewrite the direct mo-tester orchestrator

**Files:**
- Modify: `scripts/run_bvt_tenant_parallel.sh`
- Modify: `scripts/test_run_bvt_tenant_parallel.sh`

**Interfaces:**
- Consumes `--tester-dir`, `--case-root`, `--group 0|1`, `--directories`,
  `--output-dir`, optional `--resource-dir`, MySQL connection flags, and
  `--tenant-password`.
- Runs a phase using:

```bash
./run.sh -n -g -o -p "${case_root}" -i "${include_value}"
```

- Produces `serial-before.include`, `worker-0.include`, `worker-1.include`,
  `serial-after.include`, per-phase tester/report/log trees, and `summary.tsv`.

- [ ] **Step 1: Rewrite the fake tester assertions and verify RED**

Require exact directory-based invocation, fixed worker users, two concurrently
started workers, isolated resources, config redaction, and this event order:

```text
serial-before
create-account (twice)
worker-0 and worker-1
drop-account (twice)
serial-after
```

Add failure scenarios for serial-before, one worker, cleanup, leak detection,
and TERM while one worker is blocked.

Run:

```bash
bash scripts/test_run_bvt_tenant_parallel.sh
```

Expected: failures show the old planner CLI and capture logic do not satisfy the
new interface.

- [ ] **Step 2: Implement validation and include generation**

Source only the explicit `--directories` file after validating it is a regular
file. Validate all configured names with:

```bash
[[ "${name}" =~ ^[A-Za-z0-9_]+$ ]] ||
  die "invalid case directory name: ${name}"
```

Discover immediate case-root directories, ignore `optimistic`, append unknown
directories belonging to the requested group to sys-after, and write absolute
paths with trailing `/` joined by commas.

- [ ] **Step 3: Implement isolated phase execution**

Copy the tester runtime files and `lib/` into
`output/phases/<phase>/tester`, create fresh `log/` and `report/`, and copy the
resource tree to `output/phases/<phase>/resources` when supplied. Change only
the worker `user.name` and `user.password` fields before running the worker.

- [ ] **Step 4: Implement account lifecycle and failure aggregation**

Create:

```sql
CREATE ACCOUNT IF NOT EXISTS `bvtw_g0_w0`
  ADMIN_NAME 'admin' IDENTIFIED BY '111';
```

for both workers, launch each worker in its own process group, wait for both,
drop and leak-check both accounts, then run sys-after when MySQL answers
`SELECT 1`. Record every status in `summary.tsv`; return nonzero if any required
step failed.

- [ ] **Step 5: Implement signal cleanup and artifact redaction**

On `INT`, `TERM`, or `EXIT`, terminate and wait for live worker process groups
before dropping recorded accounts. Rewrite both `password:` and `syspass:`
values in every generated `mo.yml` to `"***"`.

- [ ] **Step 6: Run shell verification**

Run:

```bash
bash -n scripts/run_bvt_tenant_parallel.sh
bash -n scripts/bvt_tenant_directories.sh
bash scripts/test_run_bvt_tenant_parallel.sh
shellcheck scripts/run_bvt_tenant_parallel.sh scripts/bvt_tenant_directories.sh scripts/test_run_bvt_tenant_parallel.sh
```

Expected: syntax checks, every shell scenario, and ShellCheck pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/run_bvt_tenant_parallel.sh scripts/test_run_bvt_tenant_parallel.sh
git commit -m "feat: run BVT directories in two tenant processes"
```

---

### Task 3: Simplify reusable workflow integration

**Files:**
- Modify: `.github/workflows/e2e-compose-parallel.yaml`
- Modify: `.github/workflows/e2e-standalone-parallel.yaml`
- Modify: `scripts/test_bvt_workflow_contract.py`

**Interfaces:**
- Workflow inputs: `tenant_parallel_enabled` boolean defaulting to `false`, and
  `ci_ref` string defaulting to `main`.
- The enabled branch passes `--directories` and no planner, policy, group
  runner, or worker-count arguments.

- [ ] **Step 1: Update workflow tests and verify RED**

Assert `tenant_parallel_workers` is absent and each enabled command contains:

```text
--directories "$GITHUB_WORKSPACE/.ci/tenant-parallel/scripts/bvt_tenant_directories.sh"
```

Assert `--planner`, `--policy`, `--group-runner`, and `--workers` are absent,
while the `else` branch still invokes the original BVT runner.

Run:

```bash
python3 -m unittest scripts/test_bvt_workflow_contract.py -v
```

Expected: tests fail against the old workflow arguments.

- [ ] **Step 2: Modify both workflows**

Remove the worker-count input. Keep the feature default disabled. Replace the
old orchestrator arguments with `--directories`, retaining the correct
compose/standalone case root, tester root, output directory, group, and
standalone resource directory.

- [ ] **Step 3: Verify workflow syntax and tests**

Run:

```bash
python3 -m unittest scripts/test_bvt_workflow_contract.py -v
ruby -e 'require "yaml"; ARGV.each { |path| YAML.load_file(path) }' \
  .github/workflows/e2e-compose-parallel.yaml \
  .github/workflows/e2e-standalone-parallel.yaml
```

Expected: all tests pass and Ruby parses both YAML documents.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/e2e-compose-parallel.yaml \
  .github/workflows/e2e-standalone-parallel.yaml \
  scripts/test_bvt_workflow_contract.py
git commit -m "ci: use fixed two-tenant BVT execution"
```

---

### Task 4: Audit, trial instructions, and PR update

**Files:**
- Modify: `docs/superpowers/specs/2026-07-28-tenant-parallel-bvt-design.md`
- Modify: `docs/superpowers/plans/2026-07-29-tenant-parallel-bvt.md`

**Interfaces:**
- Produces a reviewable directory inventory and an opt-in branch trial command.

- [ ] **Step 1: Verify the directory inventory against MatrixOne main**

Export MatrixOne main, enumerate immediate case directories, and verify:

```text
sys-before: 5 directories / 29 scripts
tenant-parallel: 27 directories / 175 scripts
sys-after: 40 directories / 935 scripts
excluded optimistic: 1 directory / 22 scripts
```

Verify `analyze` and `benchmark` occur only in sys-after and every non-excluded
directory belongs to exactly one phase.

- [ ] **Step 2: Run the full local suite**

Run:

```bash
python3 -m unittest scripts/test_bvt_workflow_contract.py -v
bash scripts/test_run_bvt_tenant_parallel.sh
bash -n scripts/run_bvt_tenant_parallel.sh scripts/bvt_tenant_directories.sh
shellcheck scripts/run_bvt_tenant_parallel.sh scripts/bvt_tenant_directories.sh scripts/test_run_bvt_tenant_parallel.sh
git diff --check
```

Expected: all checks pass.

- [ ] **Step 3: Commit documentation**

```bash
git add docs/superpowers/specs/2026-07-28-tenant-parallel-bvt-design.md \
  docs/superpowers/plans/2026-07-29-tenant-parallel-bvt.md
git commit -m "docs: simplify tenant-parallel BVT rollout"
```

- [ ] **Step 4: Push and update Draft PR #409**

Push `codex/tenant-parallel-bvt`, update the PR body with the direct-command
architecture, and keep the PR in Draft while the opt-in trial is running.

- [ ] **Step 5: Trigger the shadow trial**

From a MatrixOne branch, call the reusable workflow with:

```yaml
tenant_parallel_enabled: true
ci_ref: codex/tenant-parallel-bvt
```

Compare its duration, failed cases, `summary.tsv`, and worker reports against an
unchanged run from the same MatrixOne commit. Do not merge or enable the new
path by default until coverage and cleanup are confirmed.
