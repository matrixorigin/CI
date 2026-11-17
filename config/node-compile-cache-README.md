# Incremental environment/node compilation cache (opt-in)

## What changed

The legacy reviewed archive remains unchanged and opt-out behavior is preserved.
`coverage-ut.yaml` adds `node_compile_cache: false` by default. When enabled it
uses **new** `cs-<environment>-node-cache-7c14g` pools, not the original pool.
The node-cache catalog is intentionally empty until real node bindings and
producer evidence have been reviewed. No stack or production caller is enabled
by this change.

`incremental_cache.py` implements Go's GOCACHEPROG protocol. Go supplies the
ActionID derived from source, dependencies, toolchain and build flags. The cache
does not reject an entire generation because HEAD, runner name or test partition
changed. Matching objects are copied **on demand** from an immutable shared
object store into a private job directory. New compilation writes only there.
Changing coverage instrumentation or compiler options may invalidate affected
objects; this is correct input invalidation, not an unconditional full rebuild.

Layout under each environment/node's RO PVC mount:

```
/ci-cache/incremental-v2/
  node-cache-identity.json   # identity pinned in trusted CI catalog
  objects/<prefix>/<sha256>  # immutable compiler/native bytes
  indexes/<sha256>.json      # reviewed ActionID -> output hash/size
  native/<sha256>.json       # separately keyed native generation
```

Different temporary runners on the same node can read the same objects. No
shared writable GOCACHE, job workspace, module cache, test-result cache or
credentials. Tests retain `-count=1`. Workspaces remain separate emptyDirs.

## Correctness and trust

* Runtime identity retains repository, environment, Go/CC/CXX/CMake, OS, CPU,
  Go target options and source/module paths. Whole source SHA and test group
  are **not** runtime keys; they remain producer audit evidence.
* PVC/PV/node UIDs are checked by the GitOps node pool and by a catalog-pinned
  marker plus runtime node hints and actual Linux RO mount verification.
  Pod-provided environment variables alone are not authority.
* Catalog entries pin index/native-manifest hashes and complete producer API
  metadata, immutable workflow references and artifact identity/digest. Live
  producer success and expiry are rechecked. Missing/incompatible/expired or
  unavailable metadata is a cold miss. A selected malformed index, mismatched
  identity or corrupt object is fatal, not a silent miss.
* Go may turn a cache protocol error into a miss. Therefore the helper writes
  a durable failure marker, and a separate mandatory workflow acceptance step
  rejects the result even if the tests themselves passed.
* Native cache keys cover actual tracked Makefile/cgo/thirdparties contents and
  native build/toolchain options independently from unrelated Go changes. Exact
  native hits restore hashes and input mtimes, then retain Make's original
  prepare/begin/record verification. Native misses use the original cold build.
* Go does not automatically track externally linked C library bytes. The final
  native headers/libraries' digest is included in CGO_CFLAGS; changed C libraries
  invalidate CGo actions and dependents without flushing pure-Go objects.

## Incremental publication and migration

`publish_node_cache.py` is a **separate trusted publisher**, never invoked by a
PR test job. Its caller must provide a CI-reviewed approval entry after checking
the successful trusted producer and downloaded artifact digest. Candidate claims
of success cannot self-authorize publication. The publisher rechecks live API
metadata, verifies all candidate bytes before publication, copies only new
objects under an exclusive writer lock, and publishes the immutable index last.
Existing immutable generations are retained; no overwrite/GC is performed.
`export_delta` excludes mappings already present in approved parents.

`import_reviewed_disk_cache` converts an already audited compile-only Go DiskCache
v1 without running a compiler. It preserves input-derived ActionIDs, verifies
every object, fails on unknown formats and treats trimmed objects as misses.
This is a one-time format migration of existing valid caches, **not another full
precompile**. Do not import arbitrary test-result caches from ordinary runners.
`export_native` freezes native outputs after Make without compiling again.

The protected publisher invocation, runtime catalog population and automatic
promotion from successful trusted main-branch builds still require rollout
wiring. The default workflow does not auto-publish unreviewed PR objects. The
current implementation is not evidence that all existing runner pools are live
on this path.

## Validation and rollout boundary

Local real-Go tests cover a new source commit, new private runner directories,
changed partitions with identical whole-suite coverage scope, unchanged-package
compile avoidance, changed dependencies, actual test re-execution, and external
C library invalidation. Unit tests cover integrity, provenance, expiry, private
paths, concurrent writers and delta-only publication. GitHub-hosted contract
workflow validates Linux / Go 1.27.1 without any CS workload or credentials.

MTY's separate low-resource probe validates simultaneous RO mounts and private
object copies in two real Pods plus a replacement Pod, with stable PVC/PV UIDs.
It is not a full registered-runner load or node-failure durability test.

Before activation: review catalog/marker/converted generation provenance;
validate actual MO full-suite cross-SHA behavior and native restore on Linux;
review combined old/new resources; deploy **only new** isolated pools via a
bounded deployment plan; then update the production caller/PR. No full-stack
Pulumi up, original pool mutation, main merge, 37-concurrent claim or automatic
restart of canceled monitoring is authorized by this document.

References: [Go cache program protocol](https://go.dev/src/cmd/go/internal/cacheprog/cacheprog.go)
and [Go build cache / C library caveat](https://go.dev/src/cmd/go/internal/help/helpdoc.go).
