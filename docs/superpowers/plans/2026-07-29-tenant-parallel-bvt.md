# Tenant-Parallel BVT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the directory-safe portion of each MatrixOne PR BVT group concurrently in two tenant accounts on one deployment, while preserving sys-account serial phases and the existing fallback.

**Architecture:** A Python planner converts the MatrixOne group runner's selected script list into whole top-level directory units using an explicit JSON policy. A shell orchestrator creates isolated `mo-tester` copies and tenant accounts, runs serial-before, tenant workers, cleanup, and serial-after in order, and records one artifact directory. The two reusable workflows checkout the matching CI revision and select the old or new path through an opt-in input.

**Tech Stack:** Python 3 standard library, Bash, MySQL CLI, `mo-tester`, GitHub Actions YAML.

## Global Constraints

- The smallest scheduling unit is one immediate child directory of `test/distributed/cases`; no file-level policy or worker assignment is allowed.
- The selected directories are exactly the current MatrixOne group intersected with the policy; no selected script may be omitted or assigned twice.
- Unknown top-level directories run in `serial-after` and are reported as unreviewed.
- A runtime serial-rule match downgrades the entire allowlisted parallel directory to
  `serial-after`; the planner never splits a directory by file.
- The default worker count is `2`; accepted values are `1` through `4`.
- Each workflow job continues to use one runner and one MatrixOne deployment.
- Worker accounts are removed before `serial-after`.
- `tenant_parallel_enabled` defaults to `false`, preserving the existing `run_bvt_group.sh` path.
- Test code uses only the Python standard library and fake local executables; repository tests must not require a live MatrixOne deployment.

---

### Task 1: Directory Policy and Planner

**Files:**
- Create: `scripts/bvt_tenant_policy.json`
- Create: `scripts/bvt_tenant_plan.py`
- Create: `scripts/test_bvt_tenant_plan.py`

**Interfaces:**
- Consumes: MatrixOne case root, newline-delimited selected script paths captured from `run_bvt_group.sh`, worker count, and JSON policy.
- Produces: `plan.json`, `inventory.tsv`, `serial-before.include`, `worker-N.include`, and `serial-after.include`.
- Python API:
  - `load_policy(path: pathlib.Path) -> dict[str, PolicyEntry]`
  - `build_plan(case_root: pathlib.Path, selected_file: pathlib.Path, policy: dict[str, PolicyEntry], workers: int) -> dict`
  - `write_plan(plan: dict, output_dir: pathlib.Path) -> None`

- [ ] **Step 1: Write failing policy-validation and directory-planning tests**

Create `scripts/test_bvt_tenant_plan.py` with `unittest`. Fixtures must use literal expected directories and script counts:

```python
def test_build_plan_assigns_whole_directories_and_preserves_selection(self):
    self.case("alpha/a.sql", "x")
    self.case("alpha/b.sql", "xx")
    self.case("beta/a.sql", "xxx")
    self.case("global/a.sql", "xxxx")
    selected = self.selected("alpha/a.sql", "alpha/b.sql", "beta/a.sql", "global/a.sql")
    policy = {
        "alpha": PolicyEntry("parallel", "safe"),
        "beta": PolicyEntry("parallel", "safe"),
        "global": PolicyEntry("serial-after", "global state"),
    }

    plan = build_plan(self.case_root, selected, policy, workers=2)

    self.assertEqual(plan["serial_after"], [str(self.case_root / "global")])
    self.assertEqual(
        sorted(path for worker in plan["workers"] for path in worker["directories"]),
        sorted([str(self.case_root / "alpha"), str(self.case_root / "beta")]),
    )
    self.assertEqual(plan["selected_script_count"], 4)
```

Also cover:

- duplicate directory entries rejected by the JSON object loader;
- invalid phase rejected;
- `workers=0` and `workers=5` rejected;
- a selected file outside the case root rejected;
- a partial selection from one top-level directory rejected;
- an unknown directory assigned to `serial-after` with `reviewed=false`;
- serial-before, worker, and serial-after sets are disjoint;
- longest-first directory weighting is deterministic;
- emitted include files contain absolute directory paths, never `.sql` or `.test` paths.

- [ ] **Step 2: Run the planner tests and verify RED**

Run:

```bash
python3 -m unittest scripts/test_bvt_tenant_plan.py -v
```

Expected: `ModuleNotFoundError` for `scripts.bvt_tenant_plan`.

- [ ] **Step 3: Add the explicit 72-directory policy**

Create schema version 1 with one object per directory:

```json
{
  "schema_version": 1,
  "directories": {
    "log": {"phase": "serial-before", "reason": "statement and log metadata ordering"},
    "result_count": {"phase": "serial-before", "reason": "statement result metadata ordering"},
    "sql_source_type": {"phase": "serial-before", "reason": "statement source metadata ordering"},
    "statement_query_type": {"phase": "serial-before", "reason": "statement query metadata producer"},
    "zz_statement_query_type": {"phase": "serial-before", "reason": "statement query metadata verifier"},

    "analyze": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "auto_increment": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "benchmark": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker; keep the entire directory on one worker"},
    "charset_collation": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "comment": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "cte": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "dataXtest": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "distinct": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "dtype": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "expression": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "fake_pk": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "fulltext": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "geo": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "keyword": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "operator": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "pg_cast": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "plan_cache": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "plugin": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "procedure": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "qexec": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "recursive_cte": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "replace_statement": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "sample": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "sequence": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "time_window": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "udf": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "union": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "view": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},
    "window": {"phase": "parallel", "reason": "directory scan found no tenant isolation blocker"},

    "array": {"phase": "serial-after", "reason": "contains mo_ctl"},
    "database": {"phase": "serial-after", "reason": "contains account DDL or explicit account sessions"},
    "ddl": {"phase": "serial-after", "reason": "contains account DDL, mo_ctl, or SET GLOBAL"},
    "disttae": {"phase": "serial-after", "reason": "contains account DDL or mo_ctl"},
    "dml": {"phase": "serial-after", "reason": "contains account DDL, mo_ctl, or global state"},
    "feature_limit": {"phase": "serial-after", "reason": "global feature registry and account state"},
    "foreign_key": {"phase": "serial-after", "reason": "contains account DDL or explicit account sessions"},
    "function": {"phase": "serial-after", "reason": "contains mo_ctl, account DDL, or account identity functions"},
    "git4data": {"phase": "serial-after", "reason": "branch, account, debug, and global state"},
    "hint": {"phase": "serial-after", "reason": "contains account DDL, SET GLOBAL, or system metrics"},
    "iceberg": {"phase": "serial-after", "reason": "contains explicit account sessions and SET GLOBAL"},
    "join": {"phase": "serial-after", "reason": "contains mo_ctl or account DDL"},
    "load_data": {"phase": "serial-after", "reason": "contains account DDL or mo_catalog.mo_account"},
    "metadata": {"phase": "serial-after", "reason": "contains account DDL or explicit account sessions"},
    "mo_cloud": {"phase": "serial-after", "reason": "external environment and system metrics"},
    "optimizer": {"phase": "serial-after", "reason": "contains mo_ctl or current account assumptions"},
    "pessimistic_transaction": {"phase": "serial-after", "reason": "account, restore, debug, and transaction-global state"},
    "pitr": {"phase": "serial-after", "reason": "account recovery and debug state"},
    "prepare": {"phase": "serial-after", "reason": "contains account DDL or SET GLOBAL"},
    "publication_subscription": {"phase": "serial-after", "reason": "cross-account publication state"},
    "query_result": {"phase": "serial-after", "reason": "contains account DDL or explicit account sessions"},
    "save_query_result": {"phase": "serial-after", "reason": "contains account enumeration and DDL"},
    "security": {"phase": "serial-after", "reason": "explicit account sessions and SET GLOBAL"},
    "set": {"phase": "serial-after", "reason": "contains account DDL or SET GLOBAL"},
    "snapshot": {"phase": "serial-after", "reason": "account snapshot and restore state"},
    "sql_inject": {"phase": "serial-after", "reason": "cluster-global failpoint state"},
    "stage": {"phase": "serial-after", "reason": "contains account DDL or mo_ctl"},
    "subquery": {"phase": "serial-after", "reason": "contains mo_ctl"},
    "system": {"phase": "serial-after", "reason": "system account behavior"},
    "system_variable": {"phase": "serial-after", "reason": "global system variables and account sessions"},
    "table": {"phase": "serial-after", "reason": "cluster table, account, and system metrics state"},
    "task": {"phase": "serial-after", "reason": "background task and account state"},
    "temporary": {"phase": "serial-after", "reason": "contains account DDL or explicit account sessions"},
    "tenant": {"phase": "serial-after", "reason": "tenant and cross-account behavior"},
    "tenxcloud_xx": {"phase": "serial-after", "reason": "external environment"},
    "util": {"phase": "serial-after", "reason": "contains mo_ctl"},
    "vector": {"phase": "serial-after", "reason": "contains mo_ctl or SET GLOBAL"},
    "zz_accesscontrol": {"phase": "serial-after", "reason": "account, password, and global access-control state"}
  }
}
```

- [ ] **Step 4: Implement the minimal planner**

Implement duplicate-key detection with `json.load(..., object_pairs_hook=...)`, normalize every selected path with `Path.resolve()`, map it to `relative.parts[0]`, and reject partial directory selection by comparing selected `.sql`/`.test` files with a recursive discovery of that directory.

Balance only `parallel` directories:

```python
units = sorted(parallel_units, key=lambda unit: (-unit["weight_bytes"], unit["name"]))
worker_units = [{"index": index, "weight_bytes": 0, "directories": []} for index in range(workers)]
for unit in units:
    target = min(worker_units, key=lambda worker: (worker["weight_bytes"], worker["index"]))
    target["directories"].append(unit["path"])
    target["weight_bytes"] += unit["weight_bytes"]
```

Write include files as a single comma-separated line of absolute directory paths. Empty phases produce empty files.

- [ ] **Step 5: Run planner tests and verify GREEN**

Run:

```bash
python3 -m unittest scripts/test_bvt_tenant_plan.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Dry-run the planner against MatrixOne main**

Capture both group selections with a fake `mo-tester/run.sh`, plan both groups, and verify the union contains 72 directories. At the implementation-time MatrixOne main commit `129bd689b5c415fbb448eb7b413ee84b245fb938`, the union contains 1,137 scripts.

Expected aggregate:

```text
serial-before: 5 directories, 29 scripts
parallel: 29 directories, 216 scripts
serial-after: 38 directories, 892 scripts
```

- [ ] **Step 7: Commit the planner**

```bash
git add scripts/bvt_tenant_policy.json scripts/bvt_tenant_plan.py scripts/test_bvt_tenant_plan.py
git commit -m "feat: plan BVT execution by case directory"
```

---

### Task 2: Tenant-Parallel Shell Orchestrator

**Files:**
- Create: `scripts/run_bvt_tenant_parallel.sh`
- Create: `scripts/test_run_bvt_tenant_parallel.sh`

**Interfaces:**
- Consumes:
  - `--tester-dir PATH`
  - `--case-root PATH`
  - `--group-runner PATH`
  - `--group 0|1`
  - `--policy PATH`
  - `--planner PATH`
  - `--output-dir PATH`
  - `--workers 1..4`
  - optional `--resource-dir PATH`
  - optional MySQL flags defaulting to `127.0.0.1:6001`, `dump/111`
- Produces: planner artifacts, per-phase logs and reports, `summary.tsv`, and a nonzero aggregate exit status.

- [ ] **Step 1: Write the failing shell integration test**

Use a temporary fixture containing:

- a fake group runner that invokes its tester with a literal four-file include list;
- a fake original `mo-tester` tree with `run.sh`, `mo.yml`, `run.yml`, `lib/`, `log/`, and `report/`;
- a fake `mysql` executable that appends SQL to `${FAKE_MYSQL_LOG}`;
- a fake planner implementing the real planner CLI output contract.

Assert observable behavior:

```bash
assert_order serial-before create-account worker-0 worker-1 drop-account serial-after
assert_contains "$FAKE_MYSQL_LOG" "create account"
assert_contains "$FAKE_MYSQL_LOG" "drop account"
assert_file "$output_dir/plan.json"
assert_file "$output_dir/summary.tsv"
assert_not_contains "$output_dir/worker-0.include" ".sql"
```

Add separate cases proving:

- sibling workers finish even if worker 0 fails;
- worker failure makes the final script fail;
- serial-after still runs after a worker assertion failure;
- serial-before failure prevents account creation and workers;
- account cleanup occurs on worker failure;
- an empty parallel phase skips account creation;
- generated worker `mo.yml` uses `bvtw_g<group>_w<index>:admin`;
- resource directories are copied per phase when `--resource-dir` is supplied.

- [ ] **Step 2: Run the shell test and verify RED**

Run:

```bash
bash scripts/test_run_bvt_tenant_parallel.sh
```

Expected: fail because `scripts/run_bvt_tenant_parallel.sh` does not exist.

- [ ] **Step 3: Implement argument validation and group capture**

Use `set -uo pipefail`, resolve all required paths, validate workers and group, create only children below the explicit output directory, and install a trap that drops only names recorded in an in-memory `created_accounts` array.

The capture tester parses the group runner's `-i` argument and writes one selected script per line to `${output_dir}/selected-files.txt`.

- [ ] **Step 4: Implement isolated tester preparation**

For every phase copy only:

```text
run.sh
run.yml
mo.yml
kafka.yml
log4j.properties
pprof.sh
lib/
```

Create fresh `log/` and `report/`. Copy the MatrixOne resource directory separately for every phase because `mo-tester` removes configured output paths during startup and cleanup.

For workers, replace only the YAML `user.name` and `user.password` values; retain `sysuser` and `syspass`.

- [ ] **Step 5: Implement phase execution and status aggregation**

Run serial-before synchronously. Create accounts with:

```sql
create account if not exists `bvtw_g0_w0` admin_name 'admin' identified by '111';
```

Run every tenant worker in a background subshell with `set -o pipefail` and `tee`. Wait for all PIDs without exiting on the first failure. Drop every created account, then run serial-after when MySQL is reachable.

Write `summary.tsv` columns:

```text
phase	name	status	include_file	log_file
```

Exit nonzero when a required phase failed, cleanup failed, or serial-after was skipped because MatrixOne was unreachable.

- [ ] **Step 6: Run shell tests and verify GREEN**

Run:

```bash
bash scripts/test_run_bvt_tenant_parallel.sh
```

Expected: all cases print `ok` and the script exits 0.

- [ ] **Step 7: Run planner and shell tests together**

Run:

```bash
python3 -m unittest scripts/test_bvt_tenant_plan.py -v
bash scripts/test_run_bvt_tenant_parallel.sh
bash -n scripts/run_bvt_tenant_parallel.sh
```

Expected: all pass.

- [ ] **Step 8: Commit the orchestrator**

```bash
git add scripts/run_bvt_tenant_parallel.sh scripts/test_run_bvt_tenant_parallel.sh
git commit -m "feat: orchestrate tenant-parallel BVT phases"
```

---

### Task 3: Reusable Workflow Integration

**Files:**
- Modify: `.github/workflows/e2e-compose-parallel.yaml`
- Modify: `.github/workflows/e2e-standalone-parallel.yaml`
- Create: `scripts/test_bvt_workflow_contract.py`

**Interfaces:**
- New workflow inputs:
  - `tenant_parallel_enabled`: boolean, default `false`
  - `tenant_parallel_workers`: number, default `2`
  - `ci_ref`: string, default `main`
- Existing caller contracts and fallback commands remain unchanged.

- [ ] **Step 1: Write a failing executable workflow-contract test**

Load both YAML files as text and use a small structural extractor to assert:

- all three inputs exist with exact defaults;
- the active BVT job checks out `matrixorigin/CI` at `${{ inputs.ci_ref }}` into `.ci/tenant-parallel`;
- the old `run_bvt_group.sh` command is guarded by disabled tenant parallelism;
- the orchestrator command is guarded by enabled tenant parallelism;
- compose passes its case root without a resource directory;
- standalone passes `head/test/distributed/resources`;
- artifact upload includes the orchestrator output directory.

The test must fail on the current workflows because the new inputs and guarded orchestrator call do not exist.

- [ ] **Step 2: Run the workflow test and verify RED**

Run:

```bash
python3 -m unittest scripts/test_bvt_workflow_contract.py -v
```

Expected: failures naming missing `tenant_parallel_enabled`.

- [ ] **Step 3: Add opt-in inputs and CI checkout**

Add the three inputs to both `workflow_call.inputs`. In each active job, checkout:

```yaml
- name: Checkout tenant-parallel BVT scripts
  if: ${{ inputs.tenant_parallel_enabled }}
  uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10
  with:
    repository: matrixorigin/CI
    ref: ${{ inputs.ci_ref }}
    path: .ci/tenant-parallel
```

- [ ] **Step 4: Guard fallback and tenant-parallel execution**

Keep the current group assignment and manifest generation. Replace only the final test invocation branch:

```bash
if [ '${{ inputs.tenant_parallel_enabled }}' = 'true' ]; then
  bash "$GITHUB_WORKSPACE/.ci/tenant-parallel/scripts/run_bvt_tenant_parallel.sh" \
    --tester-dir "$GITHUB_WORKSPACE/mo-tester" \
    --case-root "$GITHUB_WORKSPACE/test/distributed/cases" \
    --group-runner "${bvt_runner}" \
    --group "${bvt_group}" \
    --workers '${{ inputs.tenant_parallel_workers }}' \
    --policy "$GITHUB_WORKSPACE/.ci/tenant-parallel/scripts/bvt_tenant_policy.json" \
    --planner "$GITHUB_WORKSPACE/.ci/tenant-parallel/scripts/bvt_tenant_plan.py" \
    --output-dir "${RUNNER_TEMP}/bvt-tenant-compose"
else
  bash "${bvt_runner}" \
    "$GITHUB_WORKSPACE/mo-tester" \
    "$GITHUB_WORKSPACE/test/distributed/cases" \
    "${bvt_group}"
fi
```

The standalone branch uses `head/test/distributed/cases`, adds `--resource-dir "$GITHUB_WORKSPACE/head/test/distributed/resources"`, and writes to `bvt-tenant-pessimistic`.

- [ ] **Step 5: Extend artifact paths and summaries**

Upload the existing combined log plus the corresponding tenant output directory. Add plan phase counts and `summary.tsv` to `$GITHUB_STEP_SUMMARY` when present.

- [ ] **Step 6: Run contract and script tests**

Run:

```bash
python3 -m unittest scripts/test_bvt_tenant_plan.py scripts/test_bvt_workflow_contract.py -v
bash scripts/test_run_bvt_tenant_parallel.sh
bash -n scripts/run_bvt_tenant_parallel.sh
ruby -e 'require "yaml"; ARGV.each { |path| YAML.load_file(path, aliases: true) }' \
  .github/workflows/e2e-compose-parallel.yaml \
  .github/workflows/e2e-standalone-parallel.yaml
```

Expected: every command exits 0.

- [ ] **Step 7: Commit workflow integration**

```bash
git add .github/workflows/e2e-compose-parallel.yaml .github/workflows/e2e-standalone-parallel.yaml scripts/test_bvt_workflow_contract.py
git commit -m "ci: add opt-in tenant-parallel BVT execution"
```

---

### Task 4: Full Verification and Trial Handoff

**Files:**
- Modify if verification finds a tested defect: only files introduced or named above.

**Interfaces:**
- Produces a local CI commit SHA suitable for pinning from a MatrixOne trial branch.

- [ ] **Step 1: Run the complete local verification**

```bash
python3 -m unittest scripts/test_bvt_tenant_plan.py scripts/test_bvt_workflow_contract.py -v
bash scripts/test_run_bvt_tenant_parallel.sh
bash -n scripts/run_bvt_tenant_parallel.sh
git diff --check
```

- [ ] **Step 2: Run a real MatrixOne-main planning dry-run**

Use `origin/main:optools/run_bvt_group.sh` from `/Users/ariznawl/weilu/matrixone` and the real case tree exported from that ref. Verify both groups together produce:

```text
72 selected directories
1137 selected scripts at MatrixOne 129bd689b5c415fbb448eb7b413ee84b245fb938
5 serial-before directories
29 parallel directories
38 serial-after directories
```

- [ ] **Step 3: Inspect the final diff**

Confirm:

- no MatrixOne or `mo-tester` source file changed;
- workflow defaults preserve serial behavior;
- no password appears in `plan.json`, `inventory.tsv`, or summary output;
- cleanup targets only `bvtw_g<group>_w<index>`;
- no generated test artifact is staged.

- [ ] **Step 4: Prepare the MatrixOne trial configuration**

The caller pins both reusable workflows to the final CI SHA and passes:

```yaml
with:
  tenant_parallel_enabled: true
  tenant_parallel_workers: 2
  ci_ref: codex/tenant-parallel-bvt
```

For a non-experimental rollout, replace the branch in both the reusable-workflow `uses:` reference and `ci_ref` with the same value printed by `git rev-parse HEAD`.

Run the Compose + Proxy and Launch + Pessimistic jobs once. Compare BVT result coverage, leaked accounts, MatrixOne crashes/restarts, and wall-clock time with the serial baseline.

- [ ] **Step 5: Record final status**

Report the final local branch, commit SHA, verification commands, exact trial inputs, and the remaining push-permission limitation.
