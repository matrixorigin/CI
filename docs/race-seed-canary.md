# Race cache seed measurement

CI #456 supplies the reusable measurement workflow; a thin MatrixOne manual
workflow supplies caller context and existing CI-environment UT credentials.
No existing PR check, production default, coverage workflow or runner deployment
is changed. Implementation design: [daemonless design](race-seed-daemonless-design.md).

## Why daemonless acquisition

Read-only inspection on 2026-09-20 established the Shanghai 8c16g pool uses
ephemeral ARC pods with an emptyDir workspace, 8 CPU and 16 GiB. The observed
container has no Docker socket/sidecar/remote Docker configuration; docker info
fails. Registry transport therefore reads dedicated COPY layers using checksum-pinned
crane v0.22.1 without executing the image. An explicit empty credential configuration
prevents runner registry-auth reuse. Existing Docker transport stays unchanged.

The importer validates producer schema 2 / host-ut-v1 and Go/platform/path/flavor
contracts, imports regular cache files additively and cleans all owned blob,
tool and payload staging before publishing completion. Registry mode requires
a trusted immutable image and the known producer layer layout. Compressed blob
digests and complete uncompressed diff_ids are checked before publication. Only
metadata and build-cache layers are always acquired; module cache is fetched only
when empty. Base/native-image layers and whole-filesystem export are unnecessary.
Even these remaining transfer costs can outweigh compilation savings.

## Paired contract

- One source SHA reachable from official MatrixOne main and one immutable,
  reviewed CI harness SHA. The trusted main-branch caller pins both the reusable
  workflow and harness input to the same CI commit. CI squash merges do not
  preserve head ancestry, so admission checks checkout identity, not CI ancestry.
- Both arms use `amd64-mo-shanghai-8c16g`, canonical checkout and module paths,
  the existing Go/Java/CMake and Shanghai proxy policy, no actions Go cache restore,
  full unsharded race UT, parallelism 6 unless the existing repository variable
  overrides it, and the same UT timeout.
- A disables seeding; B uses registry seeding. No daemon or shared image cache.
  The first measured operation includes B's crane bootstrap, image transfer,
  validation, direct streaming extraction, import and cleanup, followed by
  clean/config/full UT. Per-import phase times include their cache blob downloads.
- Cold arms refuse preexisting populated Go caches. Warm arms restore the exact
  same artifact from one successful seed-off full UT. The snapshot contains no
  seed marker. Both arms invalidate test-result cache, preserving compile cache.
- CPU quota, cgroup CPU seconds/utilization/throttling, sampled memory and OOM
  deltas, host counters, disk occupancy and UT logs are retained. Host counters
  are context only: visible cpuset can be 96 CPUs while quota is 8 cores.
- Exact nonempty test and package outcome multisets must match. Empty, failed,
  truncated, skipped-import, partial-import or mismatched arms are invalid.

The matrix requests AB/BA alternation with max-parallel 1; actual scheduler order
must be checked from timestamps. Page-cache state, colocated workloads and network
conditions remain noise. First run: one warm preparation plus four measured arms.
Three repetitions: one preparation plus twelve arms. No automatic rollout decision.

Checkout/toolchain setup, snapshot production/transfer/restore, parsing and
artifact upload are outside paired UT time and visible separately in job duration.
Importer-owned cleanup is inside the measurement. No image-prune cost is deferred
to pod destruction in registry mode. Hard-kill leftovers belong only to the
ephemeral pod. Sampled peaks can miss short spikes; OOM counters supplement them.

## Publication and operation

Verified builder run 35443952690 / job 105925505882 produced source
`3ac87c30625fc082391e5384a4c94e50a2916cf6`, manifest
`sha256:8137d222d3a3b29d119173639cea98931168ab7d886bf882894391f7804d5d88`,
published to Docker Hub and the Shanghai ACR mirror. Registry metadata was checked
independently: Go 1.26.4, linux/amd64/v1, race=ok and coverage=ok. Publication and
import success are not compiler-hit evidence or end-to-end speedup evidence.

Merge order (human authorization required): CI #456 first, then the MatrixOne
manual caller pinned to the reviewed CI commit. Dispatch only on MatrixOne main
with repetitions=1. Review complete cold/warm outcomes and resource deltas before
increasing repetitions. Do not copy S3 credentials to the CI repository.

Keep ordinary seed off unless repeated measurements show stable net wall-time
benefit without correctness, memory, CPU or disk regression. No benefit or a
slowdown means no rollout. Coverage requires its own later experiment.
