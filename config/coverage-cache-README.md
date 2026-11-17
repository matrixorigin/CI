# Reviewed Coverage compilation cache

`coverage-cache-catalog.json` is a CI-owned allowlist, checked out at the
reusable workflow's immutable revision. It is not read from the MatrixOne PR.
Normal Coverage jobs only consume entries: they never create, upload, refresh,
or delete a cache. `coverage_compile_cache: false` disables consumption.

## Compatibility and trust

An entry matches the exact source repository, commit, actual CS environment and
group. Go/C/C++/CMake versions, CPU features, OS, Go configuration and absolute
build paths must also match. There is no nearest-version or restore-key fallback.
Before tests, the selected/full/instrumented package lists and coverage flags are
compared with the producer's compile-only contract. Tests still use `-count=1`.

The catalog pins the successful producer's repository, caller SHA, branch, event,
actor, workflow ID, attempt and immutable reusable workflows, plus artifact ID,
name, size, service digest, complete producer fingerprint and manifest SHA256.
The pinned manifest authenticates the inner archive and native output/input
hashes. Native source timestamps are restored only after the complete input set
and contents match. Original `make config`/`make cgo` provenance checks still run;
changed native outputs fail the job. No test results, credentials, module cache,
database data or tested PR changes are published by this consumer.

No matching entry, an expired/deleted artifact, missing read permission, or
incompatible environment means cold compilation, with **no automatic seed**.
Download transport failure also falls back before restoration. Invalid
provenance, metadata, archive contents, native hashes or scope fail closed;
restoration failure must not continue to tests or be hidden as a cache miss.

## Read permission

Grant the *calling Coverage job* `contents: read` and `actions: read`, or supply
the optional `CS_COVERAGE_CACHE_READ_TOKEN` secret restricted to Actions read on
the artifact repository. The default is the caller's `GITHUB_TOKEN`. This
workflow does not escalate permissions or use the checkout token for cache API
reads. Existing contents-only callers continue cold until a maintainer enables
read access. Never put token values in the catalog, workflow inputs or logs.

## Register once, reuse until incompatible or expired

1. A maintainer explicitly runs a reviewed, immutable compile-only producer for
   the chosen source/environment/group set. Freeze native artifacts and GOCACHE
   before any UT or S3 execution, then run the unchanged full Coverage suite.
   Do not register caches written by an untrusted PR or after running tests.
2. Require producer success, full package/skip/instrumentation/merged-profile
   validation, and cross-run native/scope checks. Capture the API identity fields
   and SHA256 of the manifest; review the full immutable producer graph, not only
   a run's green status or a user-supplied filename.
3. Add exactly one entry per source/environment/group to this catalog in a CI
   review. Bind the entire producer fingerprint, including policy and original
   run/attempt; runtime matching ignores those three producer-only fields only
   after the reviewed manifest and API provenance have been verified.
4. Validate a separate consumer run against the existing artifact. No producer
   is invoked on consumption, so repeated compatible jobs amortize the one-time
   preparation/upload cost. Different commits safely miss until separately
   registered; this v1 does not promise reuse across arbitrary PR revisions.
5. On expiry, remain cold until a maintainer explicitly prepares and reviews a
   replacement. Removing an entry revokes it in subsequent workflow revisions;
   it does not delete the artifact or interrupt active tests. Do not extend
   retention or create persistent storage automatically.

The initial three entries are MNL2 source `dbc7ca14ca8b01cfe6d9e9a151619ea4b5ba14ca`
from successful run `matrixorigin/matrixone/43870157584`. They are bounded by
GitHub artifact retention; this is reusable storage, not indefinite storage.
Other source versions/environments are not claimed validated by these entries.

## Evidence and cost accounting

Per-group readable reports include cache outcome, selected reviewed entry,
manifest and phase timings, but never upload the cache again. Use job step
timestamps for metadata/download/restore costs and the slowest group plus
queue/merge for wall time. Do not sum concurrent groups or subtract transfers
twice. Report initial producer cost separately from repeated reuse. A single
successful sub-30-minute run does not establish a stable latency guarantee.
