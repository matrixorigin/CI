# Race cache seed measurement

**Draft / not ready for the production runner experiment.** Read-only inspection
on 2026-09-20 confirmed job 105924102623 uses `amd64-mo-shanghai-8c16g`,
canonical checkout/module paths, CPU quota `800000/100000` (8 cores), and
memory limit `17179869184` (16 GiB), while its visible cpuset is `0-95`.
Consequently host CPU utilization is only context, not runner utilization.
Reports now separately calculate visible-cgroup CPU seconds/quota utilization,
throttling, sampled memory peak and OOM counter deltas. They do not attribute
an external Docker daemon's CPU to the test container, nor claim hidden ancestor
limits are known.

Pool isolation remains **unverified**: the organization runners API and the
configured Kubernetes autoscaling-runner-set listing both reject access (403).
Before replacing the hosted guard, obtain a sanitized runner template confirming
per-job lifecycle and volume ownership for checkout, Go caches and Docker storage.
Also resolve a MatrixOne-side caller that inherits existing UT credentials;
do not copy credentials into CI merely to make this draft runnable. No runner
configuration, production defaults or cache contents were changed by inspection.

The canary compares the complete race suite with seed disabled/enabled, on a
fixed MatrixOne main commit and builder digest. Production defaults stay off.
The manually dispatched workflow runs only from CI main. All harness code comes
from its immutable workflow commit. MatrixOne must be an ancestor of main,
because the suite uses the existing CI test credentials.

Each arm gets a fresh GitHub-hosted Linux x64 runner. Cold arms start without
Go caches or the builder image. Warm arms restore the **same immutable artifact**
produced by one successful full race run without seeding. This snapshot has no
seed completion marker: warm means an existing compiler/module cache, not a
previous seed import. The already-seeded marker fast path is a separate future
experiment. The matrix requests alternating AB/BA order and runs one arm at a
time; matrix scheduling is not proof of execution order. Check job timestamps.

The comparison rejects different source/harness/image identities, snapshot
hashes, runner image versions, CPU/memory/toolchains or UT settings. OS page
cache and network conditions remain noise; pair repetition measures that noise.
This initial canary supports ephemeral GitHub-hosted runners only. Use a matching
larger hosted runner via RACE_CANARY_RUNNER_LABEL if the standard runner fails
the importer space gate. It does not claim measurements for self-hosted pools.

Measured wall time starts before seed acquisition and ends after make clean,
config, full make ut and importer-owned cleanup. It includes seeder failures.
Checkout/toolchain setup, warm snapshot production/transfer/restoration, report
parsing and artifact upload are separately visible workflow preparation costs,
not part of the paired UT interval. Docker image layers remain daemon-owned,
as in the production importer; they are charged to disk occupancy and reclaimed
by ephemeral runner destruction, not by an invented image-prune step.
Host CPU counters, disk counters, memory availability, cgroup counters and disk
free space are sampled throughout the measured interval. Raw samples and UT
logs are retained. Imported entries are not reported as compiler cache hits.

A successful workflow is a **valid experiment**, not rollout approval. Both
arms must complete the same nonempty test execution multiset with identical
pass/skip/package outcomes. B must report seeded, producer race ok, imported
files > 0 and complete cleanup; any skip/fallback/partial seed is invalid. Warm
cache identity is checked before running. Cold/warm are reported independently;
one smoke pair is insufficient to establish a stable gain. No coverage result
is inferred. Keep rollout off until repeated net gains and acceptable memory,
disk and CPU costs have been reviewed.

## Initial verified image

2026-09-20: builder job 105925505882 in MatrixOne run 35443952690 published
`matrixorigin/matrixone@sha256:8137d222d3a3b29d119173639cea98931168ab7d886bf882894391f7804d5d88`
from source `3ac87c30625fc082391e5384a4c94e50a2916cf6`.
The registry metadata layer was downloaded independently and its SHA256
verified. Manifest schema 2 / host-ut-v1, canonical checkout/modules, Go 1.26.4,
linux/amd64/v1, both contract IDs, race=ok and coverage=ok match the consumer.
This proves publication/compatibility, not cache-hit rate or end-to-end speedup.

## Invocation

The sample requires the five existing S3 UT credentials (S3ENDPOINT, S3REGION,
S3APIKEY, S3APISECRET, S3BUCKET). The existing CI-repository dispatch does not
establish that credential path; replace it with a reviewed MatrixOne caller
before execution. Missing credentials reject the run; values are never printed.
The currently configured MatrixOne UT pool is `amd64-mo-shanghai-8c16g`;
this hosted-only first measurement does not establish performance on that pool.
Rollout to that pool additionally requires a matching ephemeral runner experiment.

Only after the isolation and caller blockers above are resolved and the updated
workflow is reviewed and merged, dispatch Race seed canary on main with a full
40-character MatrixOne SHA and the immutable image reference above. Start with
repetitions=1 (four measured arms plus one warm preparation run); repetitions=3
gives twelve measured arms and the same one preparation run. Review the report
artifact and job summaries before requesting a separate rollout change.

## Design scope

R2 measurement contract: isolate compiler caches, preserve full race execution,
keep fixed inputs, and fail closed on incomplete evidence. No suite selection,
timeouts, scheduling or production activation changes. The importer gets one
optional digest input restricted to its two existing trusted repositories;
ordinary callers retain their current behavior. Tests cover input rejection,
empty/truncated execution evidence, mismatched outcomes and mismatched pairing.
