# Daemonless race canary — design revision 2

## Revision 2 decision (supersedes whole-filesystem export below)

The Shanghai probe disproved the practicality of full export: 900s timeout,
18.4GB partial tar, cache payloads already present, still exporting unrelated
base-system files. No marker published, cleanup complete. Do not raise budgets.

Select only the producer's dedicated COPY layers, using the same pinned crane
binary for manifest/config/blob and the same anonymous credential isolation.
Admit only a single linux/amd64 image manifest with gzip layers, history count
matching layers and diff_ids, and the exact final six nonempty history entries
from Dockerfile.ci-builder: module-cache COPY, build-cache COPY, native-install
COPY, fingerprint COPY, warm-status COPY, manifest COPY. Any layout drift rejects
the experiment, never guesses or attempts general OCI merging. This is a narrow
producer transport contract; schema-2 files still provide semantic compatibility.

Verify pinned manifest bytes by SHA256 and config blob bytes by its descriptor.
History is an admission guard, not authority to execute commands. Select final
COPY payloads only, ignoring inherited base caches (never depending on them).
No later layer may modify these paths under the admitted suffix. Reject all
whiteouts and unexpected entries; allow only parent directories and the selected
subtree's existing regular-file/directory contract. No symlinks or image execution.

Download the small producer-manifest layer first and validate its semantic
contract before large transfers. Download build layer, verify compressed digest
and declared byte size, then stream gzip directly into the existing strict
extractor's owned staging. Hash the entire uncompressed stream through EOF and
verify config rootfs.diff_id before publication. Repeat for modules only if empty.
This removes base/native downloads, decompressed export and filtered tar copies.

Bounds remain 1080s work +45s cleanup. Metadata <=64KiB compressed/decompressed;
each cache blob <=8GiB compressed; aggregate cache-layer uncompressed bytes <=24GiB.
Admission budgets current-cache stage + one compressed blob + module destination
on their actual filesystems, with reserve. Streaming hash and bounded reads avoid
full-layer memory capture; tar headers are discarded as processed. Seeder owns
all blob/stage resources; checksum/layout/timeout/cancel/partial failure never
publishes that payload or a success marker. Existing additive/empty-only commit
semantics and Docker transport stay unchanged.

Focused proof adds layout/digest/size/whiteout/path/partial-gzip/diff-id rejection,
metadata-before-large-transfer, module-transfer omission for populated caches,
and direct strict extraction/publisher cleanup. Live final backend must acquire
and import the real pinned image in the no-Docker Shanghai probe. Record actual
timing and resource outcomes; no full-UT speedup claim from this capability test.

The earlier revision below records the rejected approach and unchanged caller,
ownership, rollout and evidence decisions. Revision 2 is reviewed before code.
GPT-6 medium design review: PASS, 2026-09-20. Explicit assumption: isolation of
omitted layers relies on the trusted producer Dockerfile, not history strings
alone. Missing subtree differs from an explicit empty module directory; full
uncompressed-stream digest verification precedes publication.

Owner: CI #456, continuing the UT cache work in CI #455 / MatrixOne #29109.
Scope: image acquisition and a manual paired experiment, not production rollout.
Trigger: acquisition crosses a registry trust/resource boundary and the canary
crosses repositories. Design review precedes implementation of this revision.

## Evidence and invariant

The Shanghai 8c16g ARC template has one unprivileged runner container, ephemeral
runner ownership and an emptyDir workspace. A live read-only probe confirmed no
Docker socket or remote Docker configuration; docker info fails. Image publication
does not establish consumer usability or any speedup.

Invariant: opt-in registry acquisition imports only regular cache entries from
the two already trusted repositories at an immutable digest, validates the same
producer contract, preserves existing entries, and publishes completion only
after owned cleanup. It never executes the image or needs cluster privileges.
Production remains default-off and existing Docker transport remains the default.

## Alternatives and decision

1. Status quo Docker transport cannot work on the observed runner.
2. Add a privileged DinD sidecar: changes infrastructure, resources and isolation;
   rejected for this measurement task.
3. Implement OCI authentication/layer/whiteout logic ourselves: larger security
   and maintenance surface; rejected.
4. Use upstream crane's merged filesystem export: selected. It implements OCI
   layer/whiteout semantics without a daemon. Cost: downloads the full image and
   retains one bounded uncompressed tar; measured acquisition includes tool setup.

Reference: google/go-containerregistry v0.22.1, crane export and OCI Image Spec.
Linux amd64 release archive SHA256 is pinned to
0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0.
No mutable executable download, image execution or shell evaluation of inputs.
Design review: GPT-6 medium PASS (2026-09-20), with the refinement below:
write an explicit `{}` to task-owned DOCKER_CONFIG/config.json before every
crane invocation so the default keychain cannot fall back to Podman credentials.

## Ownership, transitions and bounds

Registry mode is explicit and requires the existing trusted digest input. One
Seeder owns all staging directories; existing inode-checked subprocess cleanup
and 1080s work +45s cleanup deadline apply on success, error and SIGTERM.
Bootstrap archive <=32 MiB, metadata <=64 KiB, export <=24 GiB; same-filesystem
free-space admission reserves export plus extracted data plus 4 GiB. Crane uses
an empty Docker config and task-owned XDG runtime/config dirs (no registry auth
reuse); subprocess tree is bounded/reaped by the existing command runner.
Export tar is read without extraction. Only manifest and the two known cache
subtrees are selected; links, traversal and special files within selected paths
are rejected by the existing strict extractor. All other rootfs paths are ignored,
not written. Export reaches EOF before successful import; incomplete downloads
never publish a marker. Original Docker path is unchanged for existing callers.
Cache publication remains additive; module cache replaced only when empty.
Peak disk admission includes export and extracted payloads, with no filtered-tar
copy: reuse the strict extractor with an explicit source prefix over the seekable
export. Release staging between build/module import, release export at cleanup.
No shared Docker/image/cache pruning. Kernel hard kill relies on ephemeral pod
teardown; no persistent deployment is created.

## Experiment integration

Run in MatrixOne caller context to inherit existing CI-environment S3 secrets.
A thin manual workflow calls this reusable workflow at an immutable CI commit.
The trusted main caller pins workflow and checkout to the same reviewed SHA;
checkout identity is enforced, but CI ancestry is not required because this
repository squash-merges PRs. This avoids invalidating a reviewed immutable pin.
Harness checkout explicitly pins its own workflow revision; source SHA is an
ancestor of official MatrixOne main. Samples use the observed Shanghai pool,
canonical source/module paths and job-local compiler cache. Never clear an
unknown populated path. Warm preparation exports a common artifact; each warm
arm restores identical bytes, and both invalidate test-result cache. Record
source/harness/image identity, cgroup quotas, toolchain, cache hash, CPU/memory,
disk and full test outcome multiset. A/B sequential, one warm preparation plus
four measured arms for the first run; no coverage or rollout inference.
Avoid contaminating initial caches with tool compilation: bootstrap a verified
binary inside measured seed acquisition only. Toolchain/source preparation is
identical in A/B; no GitHub Go cache restore. Job durations expose setup overhead.

## Verification and rollout

Deterministic tests: trust/pin validation, download/export limits, traversal/links,
manifest mismatch, partial export, unchanged existing cache, cleanup/cancellation,
real nonempty paired test evidence and workflow source/caller identity. Reuse the
existing importer tests; run Linux tests and actionlint. Independently validate
the pinned release checksum and live registry metadata, then exercise actual
image import in an owned Linux environment without a Docker socket. Final review
uses GPT-6 medium. Full A/B only runs through authorized manual dispatch after
entry workflows are available; do not merge PRs without authorization or wait for
unrelated CI. Missing full A/B means no measured speedup claim or seed rollout.

Rollback: keep seed off; remove opt-in workflow/backend without cache migration.
Known cost risk: full filesystem export may erase compile savings; that is a valid
negative result. No promised percentage improvement. Any incomplete arm is invalid.
