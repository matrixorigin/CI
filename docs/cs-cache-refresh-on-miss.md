# Refresh a missing CS compile profile (draft, disabled)

Coverage tests keep running on misses. A separate MatrixOne workflow listens for
completed workers and invokes the pinned CI receiver. The hosted job can only read;
it does not seed a PVC or change test selection or scheduling. The receiver alone
is **not an installed refresh service**.

1. By default (opt out with `CS_CACHE_REFRESH_OBSERVE=false`), a CS
   worker records its environment, node and volume UIDs, runtime digest, native
   inputs key, root go.mod/go.sum blob fingerprint and compile recipe. The
   existing report contains the actual shared Go reads, compiler calls and native
   hit/miss. Failure to collect the optional hint does not fail tests. No compiler
   outputs, credentials or suggested executable paths are exported by the observer.
2. `cache_refresh_worker.py` verifies the **successful worker, workflow
   revision, parent grant, run/attempt, runner, group and artifact digest**.
   A complete hit skips maintenance. A cold read or partial rebuild can request
   an independent inventory check; neither alone permits a shared write.
   The ZIP is untrusted even after authentication. The controller must independently
   inspect the approved volume, live revocations, accepted generations and the
   protected upstream source. The optional `plan` command is read-only.
3. A normal partial Go rebuild is not by itself a reason to republish. A change
   only present in a fork waits until a trusted source has the same profile. When
   that source becomes available the ingester must reconsider waiting hints.
   Initialize the module inventory from verified prior generations, not from an
   empty database interpreted as “every node is missing”. A replaced PVC gets a
   different key. Two reports for the same profile on the same volume share one
   durable intent; different volumes do not share an intent.
4. Reuse an independently verified, trusted candidate if available. Otherwise a
   disposable, read-only-volume Linux builder can run `scripts/cache_refresh_prepare.py`.
   It expects pristine, separate upstream MatrixOne and pinned CI checkouts and
   private cache state prepared with the pinned helper (same paths/flags as the UT
   workers; set `PYTHONDONTWRITEBYTECODE=1` before using the helpers). The candidate
   covers the **union of both groups**, uses Go's incremental compilation and
   does not run UT or write the shared mount. The toolchain and Go module
   download caches are not the objects published by this feature.
5. Independently verify the producer, immutable index, native manifests, object
   inventory and hashes. A separate, node-scoped writer appends only missing
   objects; verify them back through a separate read-only mount before activating
   a reviewed catalog generation. Do not overwrite or un-revoke old generations.
   Matching readers can then use the new generation; a different volume needs
   its own verified publication.

## Operational integration still required (cloudsigma-gitops)

Restrict runner group `cs-cache-maintenance` to the authorized receiver; install
the exact pinned CI code in its read-only image at `/opt/matrixorigin/cache-refresh/ci`
with sibling `ci-revision`, and provision a durable private ledger directory at
`/var/lib/matrixorigin/cache-refresh`. Configure the protected environment
`cs-cache-publish` and the reviewed adapters **before** setting MatrixOne's
`CS_CACHE_REFRESH_CONTROLLER_READY=true`. Until then the receiver reports the
miss but never schedules a write. The controller rechecks the original evidence.
The fixed protected adapters under `/opt/matrixorigin/cache-refresh/bin/` exchange a single
JSON document on stdin/stdout. `inspect` returns an independently constructed
profile, pinned upstream commit and admission/missing flags; `prepare` launches
the private builder described above and captures its final receipt. Remaining
operations are `verify_candidate`, `append_missing`, `readback`, and `activate`.
Each must independently enforce its role and return the key, operation,
upstream commit, success and immutable index digest. A receipt alone is not
proof of authorization. Existing fixed-generation writer approvals do **not**
authorize automatic publication of a different runtime.

Use one protected controller with private durable SQLite storage (not the
shared compiler cache or a PR runner) and fresh authorization at publication
and activation. An ambiguous operation is left at `*:intent`; inspect the
publication and ledger by key before manual recovery. Do not delete an intent
and retry blindly. Turning off observation stops new hints; disabling the
controller stops new publications. Rollout should start with one volume:
prove two duplicate hints yield one candidate, unchanged old generation,
independent readback, and a subsequent real PR with documented shared hits.
No live acceptance or production deployment is implied by unit tests.

## JED rollout and acceptance (off by default)

Enroll only `jed-n0` and/or `jed-n1` on protected controller runners with
matching `cs-cache-refresh-controller-<target>` labels. Set the repository
variable `CS_CACHE_REFRESH_TARGETS` to a JSON list of those names. Use
`CS_CACHE_REFRESH_READ_SITE=jed` only after GitOps installs the separately
owned **read-only ConfigMap directory** at `/run/matrixorigin-cache-releases` on
the selected node's readers. Never project it from the compiler-cache PVC or
give PR jobs permission to patch it. Unenrolled sites keep their existing behavior.

The release record is keyed by the full node/volume/runtime/native/module/recipe
profile, with an exact verified index, native manifests, manifest digest and
trusted upstream SHA. The publisher must independently rehash the physical
objects from a read-only mount before a compare-and-swap on the pre-enrolled
ConfigMap. The consumer verifies the live base authority and revocations before
reading any new generation. Revoking its release digest, index, base authority,
or site stops new selection. Delete the projection record to retire the profile;
a stale projection is not a substitute for the live revocation check.
ConfigMaps are bounded (1 MiB); perform protected retirement of unused profiles
before filling one. Never merge permissions for reader and writer roles.

A completed private prepare can wait for independent artifact registration.
The controller polls inventory for up to five minutes; a later eligible worker
can resume the saved index for that same upstream/profile. No ambiguous dispatch
or write is automatically replayed. Distinct volumes must verify their own copy;
reuse a candidate across volumes only if independently admitted for the target.

For a **subsequent** successful real PR inspect the group report's
`cs-node-cache/release-consumption.json`. Match its profile and index to the
release. `released_shared_go_reads` only counts a shared Get for an action
absent from all original layers, not a private re-read or Put.
`released_native_hit` requires an actual native hit that wasn't available in
the original selection. A selection receipt, UT success or aggregate hits alone
are not evidence of reuse. Stop new writes with CONTROLLER_READY=false;
unset READ_SITE (or revoke) to stop new reader selection.
