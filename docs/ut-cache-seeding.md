# Correct, measurable UT cache seeding

For the opt-in daemonless Shanghai race experiment, see
[race-seed-canary.md](race-seed-canary.md). `transport: registry` requires a
trusted digest-pinned `image`; it uses checksum-pinned crane COPY-layer reads rather than
a Docker daemon. Existing action callers still default to `transport: docker`.
No production workflow enables the new transport or seeding automatically.

## Problem and scope

The race and coverage UT workflows both consider a nonempty GOCACHE warm.
`go env`, invoked by setup-go, initializes this directory (256 shards, README,
and small compiler-probe entries) without compiling the UT workload. A fresh
Go 1.25.4 cache reproduced this in 44 KiB. Four successful Linux Go 1.26.4
race jobs on 2026-09-19 all skipped seeding; their light phase took 13–19 minutes.
Those different-PR measurements are a baseline, not a paired performance test.
The subsequent empty-directory rename also cannot import into initialized caches.

This correction owns only the existing cache-import boundary, common to both
consumers. Race instrumentation, scope, admission and parallelism are unchanged.
Race UT already clears test results; coverage explicitly uses `-count=1` so
imported or persistent test success records cannot bypass actual execution.
Native libraries are never imported. No SQL behavior changes;
SQL BVT is not applicable.

## Producer/consumer contract (schema 2, host-ut-v1)

Go's non-trimpath action keys include source directories. Merely importing a
cache built under `/go/src/github.com/matrixorigin/matrixone` into a host checkout
does not reuse the MatrixOne compilation. The companion producer keeps its two
normal `make build` operations at the existing container path, then **moves**
the source to `/home/runner/_work/matrixone/matrixone` and modules to
`/home/runner/go/pkg/mod` before race/coverage warming. Final image module and
native-install destinations stay unchanged for container consumers. Do not add
global `-trimpath`: existing tests locate fixtures through `runtime.Caller`.

Consumers reject noncanonical `GOMOD`/`GOMODCACHE` before any Docker call. After
acquisition, a bounded, uncompressed metadata tar must contain
`/mo-prebuilt/go-cache-manifest.json` matching schema, profile, checkout, Go
version/platform/experiment/module path and both compile-flavor contract IDs.
Missing, legacy, malformed, oversized or mismatched manifests skip all payloads
and do not create a completion record. Manifest paths describe compilation, not
final image storage paths. Flavor `FAILED` permits visible partial import;
unknown status does not. Go still decides individual compatibility, including
CGo flags/toolchains; a manifest match is not proof of a hit.

## Import contract

Use a trusted CI composite action and its GITHUB_ACTION_PATH helper, never a
helper from the MatrixOne PR checkout. Import Go build cache entries additively:
validate and stage the complete payload, then atomically install missing regular
files without overwriting existing data. Record a completed import only after
publication; it is NOT a measured compiler hit. Go determines entry compatibility.

A versioned completion record includes the consumer environment/flavor, immutable
image identity, producer flavor health and import statistics. Same-policy,
same-environment repeats avoid download/extraction. Partial/unknown producer warm
health remains visible and is not reported as full workload warmth. Changing
policy generation permits a deliberate refresh; nightly tag updates do not
force every persistent runner to reimport gigabytes.
Go's `clean -cache` does not remove the top-level completion record. To explicitly
reseed after cache trimming/cleaning, remove `.matrixone-seed.json` or bump the
action's generation; a record is evidence of past import, not current warmth.

Preserve populated module caches and do not extract their payload. Empty module
caches may be populated only from a completely staged payload. Ordinary Go
downloads fill missing modules. Never merge partially published module trees.

## Ownership and limits

The runner job owns its Go cache paths exclusively while seeding, before tests.
A bounded seeder lock prevents competing imports; arbitrary concurrent Go writers
are not supported. Staging directories and stopped containers have invocation
identity and one cleanup owner. No registry login, image execution, shared prune,
or removal of preexisting cache data/image tags is allowed.

Anonymous pulls retain the existing registry locality/fallback policy. Preflight
space checks use actual cache and Docker storage paths; bounded pulls and a total
deadline prevent unlimited retries. These checks are NOT filesystem quotas:
another writer or growing image can exhaust disk, which must fail safely.
Docker image layers remain daemon/operator-owned, including partial pulls; shared
storage reclamation is the runner operator's responsibility. Hard process kill
may require runner teardown to remove owned staging/container leftovers.

The explicit ownership registry releases a temporary directory only after
confirmed deletion. Creation/registration, module rename and marker publication
defer soft-signal exceptions but remember cancellation. Recursive deletion runs
in a separate process group, checked against the registered path/device/inode;
the importer never recursively deletes in-process. TERM/KILL and reaping are
bounded, including repeated TERM/INT/ALRM. A failed middle-stage deletion stops
further import and retains ownership for one final retry. Build staging must be
removed before module staging starts. Anonymous Docker config survives the last
container-removal attempt. Published complete cache files/modules are not
temporary resources and are never removed on failure.

Work has an 1080-second budget; cleanup shares a separate 45-second accumulated
budget, at most 15 seconds per attempt including termination/reaping. The budget
does not reset on signals or retries. Final reporting distinguishes cancellation,
import failure and incomplete cleanup and lists leftover paths/containers/PIDs.
Kernel-uninterruptible processes and hard kills cannot guarantee reclamation.
A completion marker is committed only after required cleanup, under the seeder
lock, without pending cancellation; failed/cancelled imports never publish one.
The final commit checkpoint is the completion boundary: cancellation detected
there rolls publication back; a signal after it retains the completed result
and marker. Both sides are covered by real-signal regression tests.

## Alternatives and rollout

Directory/file-count/size thresholds remain warmth heuristics. Always overwriting
cache directories loses useful work. A new isolated Docker service or cache-only
artifact distribution could reduce transfer cost, but is a separate infrastructure
change. This fix reuses the existing trusted producer and transport.

Both reusable workflows default cache import **off** (`ut_cache_seed: false` in
`ci.yaml`, `cache_seed: false` in `coverage-ut.yaml`). Off means no image pull or
import. Merge the consumer interface before adding these inputs to callers;
then publish the companion producer image and opt in only selected canaries.
Legacy images are safely rejected. Keep default-off until a same-SHA, same-runner,
same-Go-1.26.4 comparison includes acquisition, import, cleanup and UT execution,
with cold and persistent caches, CPU, wall, disk, and unchanged test counts.
Global rollout is a separate evidence-backed decision, not part of this fix.
Rollback disables the input. Local
tests use tiny deterministic payloads/fake acquisition and inject failures rather
than downloading the multi-GB image. Cover initialized/nonempty caches, repeated
imports, malformed archives, failed transfer/publication, space/deadlines,
flavor identity, module preservation and owned cleanup. Before claiming speedup,
run same-SHA Linux Go 1.26.4 canaries and include import time, compilation/total
wall, CPU, disk and unchanged test counts. No end-to-end speedup is yet measured.

## Design and validation record

Implementation: https://github.com/matrixorigin/CI/pull/455. The earlier review
was superseded by two reproduced gaps: relocated compiler keys and soft-signal
interruption of TemporaryDirectory cleanup. GPT-6 medium design session
`01a0b98f-bf7e-72a3-9728-9d6d6092dc0f` approved the corrected cross-repository
contract and explicit bounded ownership design before implementation.

`test_seed.py` covers additive import, no-clobber, archive validation, space,
module preservation, marker identity and manifest rejection. `test_cleanup.py`
uses real isolated processes and SIGTERM/SIGALRM barriers during build/module
cleanup, TERM-ignoring helpers, ownership transitions and changed identities.
`probe_go_cache_paths.py` is an opt-in real-Go fixture with a module-cache
dependency: matching paths must reuse compilation; relocating checkout/modules
independently demonstrates corresponding misses. Every invocation executes the
test body with `-count=1`. These tests do not establish end-to-end MO speedup.

Final independent GPT-6 medium review: PASS, session
`01a0b9a0-9ec3-75a0-b89a-9ec6565fba04`. Reviewed CI implementation tree
`e79eccf3a86d72d9f74f6a09145430ed8742785d` and producer tree
`9e088cb9019a6e07d3678a5ec10b4b2cd87b7328`; subsequent changes are delivery docs.
Evidence: 29-test Linux importer suite plus four affected post-review checks
(31 distinct tests overall), three Linux producer tests, actionlint, and the
real-Go path fixture. The final-checkpoint finding is closed on both sides of
the commit boundary. Full Linux MO canary remains required before enabling.
