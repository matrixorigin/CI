# Race seed validation record

Scope: CI #456 against main `bff80f0fed2739725cd7cd256d6920875e871fc8`,
daemonless adaptation against prior head `6da76ff`, plus the MatrixOne manual
caller. Production seed remains off. No kernel/SQL behavior changes, so kernel
UT/BVT is not a substitute for these importer and workflow checks.

Design-first review: GPT-6 medium approved daemonless design revision 1 before
implementation, requiring explicit empty Docker config rather than merely an
empty directory. Final caller pins will use the exact reviewed CI commit.

| Closure | Risk / invariant | Evidence |
|---|---|---|
| Registry acquisition / strict import | R3 trust boundary; trusted digest, pinned binary, no auth reuse/image execution | checksum/auth/pin/path/PAX/platform tests; real acquisition probe |
| Temporary files / subprocesses | R3 ownership, cancellation and capacity | existing repeated-signal/bounded-reaper tests plus partial registry acquisition cleanup |
| Runner preparation / snapshot restore | R2 preserve cache, correct roots across filesystems | cold/populated guard, forced EXDEV warm restore, exact bytes/layout |
| Caller / immutable source / comparison | R2 credentials and measurement identity | official-main source rejection, exact harness identity, outcome multiset tests, workflow lint |
| Production defaults | R1 compatibility | unchanged Docker default and opt-in seed; prior importer regression suite |

| Audit | Owner / termination / bound |
|---|---|
| Q1 | Seeder registers staging inode at creation, deletes only owned paths, commits marker after cleanup. Pod teardown owns hard-kill leftovers. |
| Q2 | Existing command process-group reaper; 1080s total work, 45s shared cleanup; no unbounded network subprocess. |
| Q3 | 32 MiB tool archive, 64 KiB metadata, 8 GiB compressed cache blob, 24 GiB aggregate uncompressed cache layers, free-space admission; no full export or global tar header index. |

Linux Python 3.12: 43 importer/cleanup/registry tests and 11 canary tests pass.
Regression includes safe populated-cache rejection and cross-filesystem warm
restore using real shutil.move fallback. actionlint v1.7.12 checks both reusable
workflows, the validation workflow and the MatrixOne caller. Diff whitespace clean.

Live capability validation uses a task-owned, deadline-bounded Shanghai pod with
the observed runner image digest and 8 CPU / 16 GiB, no Docker socket and no
service-account token. It does not register as a GitHub runner or run full UT.
The Go 1.26.4 toolchain archive is independently checksum-verified. Producer
source is `3ac87c30625fc082391e5384a4c94e50a2916cf6`; image manifest is
`sha256:8137d222d3a3b29d119173639cea98931168ab7d886bf882894391f7804d5d88`.
The rejected full-export backend timed out after 900s (902.205s including setup
and cleanup), with 18.4GB partial output, zero imported bytes and complete owned
cleanup. The diagnostic-only retained hardlink was explicitly removed. No OOM
was recorded, but cgroup memory.max reclaim events occurred. This negative result
led to reviewed design revision 2: direct cache-layer acquisition/extraction.
Final-backend live outcome is recorded before delivery; no passing claim is made
here for an in-progress probe.

Missing by design before merge/dispatch: complete cold/warm A/B race UT and
net-speedup evidence. Successful import or local tests cannot justify rollout.
