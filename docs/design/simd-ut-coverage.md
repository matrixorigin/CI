# Production-mode SIMD unit-test coverage

Owner: [MatrixOne issue #32325](https://github.com/matrixorigin/matrixone/issues/32325).
The requester approved this two-PR design before implementation: first add the
shared coverage input, then enable it in a separate MatrixOne CI-only PR.

## Problem and contract

MatrixOne's production build enables `GOEXPERIMENT=simd` and `GOAMD64=v3` on
amd64. The dedicated UT coverage producer invokes Go directly, bypassing MO's
test script and excluding SIMD-tagged implementations and tests. Expression
PR [matrixorigin/matrixone#32237](https://github.com/matrixorigin/matrixone/pull/32237)
therefore fails changed-code coverage despite existing backend tests.

Add boolean `simd_coverage`, default `false`, to `coverage-ut.yaml`. Omitted or
false preserves the caller's environment. True selects `GOEXPERIMENT=simd` and
`GOAMD64=v3` before discovery, instrumentation, native preparation, and test
execution. Log compiler, OS, architecture, experiment and architecture level.
The explicit request is never silently retried in scalar mode.

The workflow step owns these process-local environment exports. It does not
write persistent Go configuration. The existing coverage runner inherits them;
its execution, cancellation and cleanup contracts need no changes.

## Decisions and bounds

Keep opt-in local to the coverage workflow. A global SIMD default would change
legacy callers. Changing only MO's test script cannot reach this producer.
Duplicating a coverage lane or adding SQL cases for internal permutations would
increase cost without correcting the build-mode mismatch.

Keep package scope, assertions, coverage thresholds/exclusions, artifact names
and generations, deadlines, permissions, and eligibility checks unchanged.
The ordinary MO UT lane remains scalar. Go validates explicitly requested modes;
unsupported requests fail before producing an eligible profile. Existing Go
cache keys distinguish compiler/experiment/architecture modes; no cache-key or
SDK changes are required. Record actual producer duration/cache observations.

This adds one workflow input, no runtime API, resource owner, background process,
kernel change, database format or migration. The change introduces no additional
all-package test execution.

## Validation

Run fast contract tests for omitted/false/true input, preservation of ambient
values, ordering before discovery and native preparation, inheritance through
the actual runner, and failure propagation. Tests use fake Go commands because
the CI validator runs on arm64 with Go 1.26.4. Enforce them through its existing
workflow validator, alongside schema validation and actionlint.

The MO caller PR adds only `simd_coverage: true` to the main-branch coverage call.
It starts from main independently of #32237. Native/SIMD execution is validated
by MO's supported amd64 coverage runner, not by the CI repository's validator.

## Rollout and completion

1. Merge the shared-CI prerequisite while existing callers retain default-off.
2. Publish and merge the separate MO caller PR after the shared ref accepts the
   input. Link both PRs to #32325 using the PR template.
3. Merge newest authoritative MO main normally into #32237 and trigger one
   fresh CI generation. `pull_request_target` runs the base workflow, so the
   caller PR's own pre-merge CI cannot prove opt-in execution.
4. Verify source/mode identity, SIMD test execution, ordinary scalar UT, and
   merged UT+BVT changed-code coverage strictly greater than 75%. Publish the
   real result before closing #32325. Local function-only coverage is supporting
   evidence, not a replacement for the required verdict.

The MO opt-in can be reverted independently while the shared input remains
default-off. Rollback does not waive #32237's required coverage check. New
performance optimizations and #32266's known regressions remain separate work;
CI-only configuration does not require another TPC-H comparison.
