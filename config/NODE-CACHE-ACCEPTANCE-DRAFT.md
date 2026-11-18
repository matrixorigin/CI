# Isolated full-suite activation gate

`node-cache-full-acceptance.yaml` is an isolated experiment, not production
activation. The original live caller and runner pools are unchanged. The
optional production-workflow code exists but is not enabled by this experiment.

As of 2026-11-18 01:59 UTC, the initial full-suite runs for MTY 44117113781,
MNL2 44117281094, JED 44208196494 and RUH 44210497665 have passed independent
raw-evidence review. These runs use existing
Go cache objects plus private missing-object compilation, not a wholly warm
native restore and not controlled-new-SHA acceptance.

Protected incremental append is implemented separately from test jobs. Both
MNL2 deltas, both MTY deltas and both JED deltas are independently verified and published;
their catalog entries explicitly select the reviewed group-zero native outputs.
RUH group-zero append 44219823683 is pending; no RUH append entry is activated.
Do not activate any further candidate catalog until
every named publication receipt, immutable binding, and cleanup is verified.
The additional warm/native/source-canary acceptance checks are implemented,
but their new helper/catalog pins have not been activated in the caller.

The initial general-cache full-suite eligibility-to-final wall times are
45m46s (MTY), 50m02s (MNL2), 42m59s (JED), and 51m33s (RUH). They include
native misses and private incremental compilation, and must not be presented
as the warm-restore timings. All four reviews checked 505 packages, 484
instrumented packages, 86 exact skips, 446657 profile blocks and mandatory
same-environment S3. The changed-runner/partition/source acceptance is still
pending; these initial results alone do not establish it.

Before activation:

1. Independently audit each protected initial publication receipt and exact
   environment/node/PVC/PV identities; commit only those published bindings to
   `node-compile-cache-catalog.json`.
2. Safely create new node-bound reader pools with fresh combined resource
   budgets. Each may have multiple temporary runners, sharing only a read-only
   cache mount and separate mutable work directories.
3. Commit the complete two/three-group helpers and populated catalog, then
   replace every helper/policy checkout pin in the isolated workflow with that
   immutable commit. A workflow file on a newer branch is not evidence that
   an already running caller uses the new helper or catalog.
4. Add an existing-branch caller only after its prior CodeQL completes and its
   remote head matches. Do not alter or cancel current runs.
5. Run full same-source acceptance, review compile-only delta artifacts, and
   publish only new objects through a protected append path. Legacy native
   manifests lack v2 tool-hash evidence and are not relabeled as native hits.
6. Review a controlled changed-source commit into the source allowlist, then
   independently accept fresh runners, changed grouping and changed SHA in
   all four environments. Fixture tests or conversion alone do not prove this.

The shared graph uses Go input-derived ActionIDs, not SHA or group as its cache
key. Native changes retain independent input/toolchain/content checks. The
probe requires a real approved parent generation; production cold-miss policy
remains unchanged. Full tests execute with count=1, p=7, original timeouts,
same-environment S3 and complete scope/profile/skip evidence.

Each isolated node-zero pool is currently capped at two runners. A three-way
partition therefore includes real scheduling wait; it is not evidence of
three simultaneous runners, all-node rollout, or 37-runner capacity.
