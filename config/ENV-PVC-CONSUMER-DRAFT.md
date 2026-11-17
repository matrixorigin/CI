# Local draft: environment-bound PVC consumer

This branch is not deployed and has no usable reviewed catalog entries yet.
The copied MNL2 catalog under scripts/fixtures is only a unit-test fixture; the
runtime catalog is empty. Never populate it by changing an old entry's environment.

The consumer extends the reviewed restore helper with an explicit environment,
group and dedicated runner-pool binding. Source, toolchain, CPU, paths, native
input/content/mtime, complete archive, producer identity and coverage scope checks
remain mandatory. The cs_environment fingerprint field is never omitted when
comparing compatibility.

Local CacheTests, PvcEnvironmentBinding and workflow contract: 17 tests passed.
The MTY-only workflow coverage-mty-pvc-consumer.yaml has been wired locally and
passes actionlint, using unchanged shared Coverage helper 46709897 and pinned
environment-aware restore helper f6f2424c. Its catalog is intentionally empty,
so mandatory-hit validation blocks testing until reviewed artifacts exist.
It compares against MTY baseline 44032472330, including environment/toolchain/
CPU/path/helper-policy fingerprints, all 505 packages, 484 instrumentation
packages, skips and coverage profile blocks. No consumer has been dispatched.
Do not call this an end-to-end validated implementation.

Next: accept the successful MTY producer run and its immutable references; audit
its manifest/artifact digests; add reviewed entries; populate only the existing
experiment PVCs into new directories; wire the readonly PVC workflow and compare
against MTY baseline 44032472330. JED and RUH require their own successful producer
and baseline evidence. Keep count=1, all packages, S3 smoke, profile/skip acceptance.
