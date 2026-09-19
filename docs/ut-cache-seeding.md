# Correct, measurable UT cache seeding

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

## Contract

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

## Alternatives and rollout

Directory/file-count/size thresholds remain warmth heuristics. Always overwriting
cache directories loses useful work. A new isolated Docker service or cache-only
artifact distribution could reduce transfer cost, but is a separate infrastructure
change. This fix reuses the existing trusted producer and transport.

Both workflows use the same action; rollback reverts those references. Local
tests use tiny deterministic payloads/fake acquisition and inject failures rather
than downloading the multi-GB image. Cover initialized/nonempty caches, repeated
imports, malformed archives, failed transfer/publication, space/deadlines,
flavor identity, module preservation and owned cleanup. Before claiming speedup,
run same-SHA Linux Go 1.26.4 canaries and include import time, compilation/total
wall, CPU, disk and unchanged test counts. No end-to-end speedup is yet measured.

## Design review

GPT-6 medium agent 01a0b971-87c4-77b3-bc22-bcb4a632c0a1 reviewed the causal
boundary at CI e43e707. Shared-daemon resource-limit amendment approved before
implementation: preflight and deadlines, not hard quota guarantees; no image
deletion. Same-filesystem atomic publication and accurate partial markers required.
Implementation PR: pending.

Overall implementation review: PASS, same GPT-6 medium session, after 22 Linux
contract tests, six coverage-wrapper tests and actionlint passed. Local real-Go
race smoke reduced compiler invocations from 120 to zero while executing the
test body in both runs. Full MatrixOne Linux canary and image-transfer overhead
remain unmeasured; this is a correctness fix, not a claimed end-to-end speedup.
