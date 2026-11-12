# CS S3 configuration for UT Coverage

The shared CS large runner label may schedule a job in MTY, MNL2, JED or
RUH. Configure `CS_S3_MTY_CONFIG`, `CS_S3_MNL2_CONFIG`,
`CS_S3_JED_CONFIG`, and `CS_S3_RUH_CONFIG` as GitHub Actions secrets in the
`ci` environment. Each secret is a CSV-style test spec, for example:

```
name=cs,endpoint=https://s3.example.invalid,region=us-east-1,bucket=ci-test,key-id=ACCESS_KEY,key-secret=SECRET_KEY,is-minio=true
```

Use a dedicated test bucket or a separately authorized `ci-ut/` prefix and
credentials scoped to that test data. Never paste real credentials into this
file or workflow logs. All four configurations are required before enabling
the shared runner label; the selected runner's name chooses exactly one.

## Scheduling and storage binding

The production default remains `runs-on: cs-large-7c14g`, **not a pinned JED
runner**. GitHub selects one eligible large runner across the CS environments.
The entire Coverage job, including its eight parallel Go packages and S3
subtests, stays on that runner. Tests are not independently scheduled to other
runner environments. A second PR/job may land elsewhere and must select its
own configuration again.

| Actual runner name prefix | Configuration |
| --- | --- |
| `cs-mty-` | `CS_S3_MTY_CONFIG` |
| `cs-mnl2-` | `CS_S3_MNL2_CONFIG` |
| `cs-jed-` | `CS_S3_JED_CONFIG` |
| `cs-ruh-` | `CS_S3_RUH_CONFIG` |

Unknown runner names and missing matching configuration fail explicitly. There
is no fallback to another environment's storage, and the job summary records
the selected environment and isolated prefix without printing credentials.
If Coverage is split into multiple jobs in the future, every job needs this
binding; a configuration selected by a previous job must not be reused.

JED storage may be cluster-internal only. In that case use its internal S3
endpoint and run on a JED runner that can resolve and reach that endpoint.
Public exposure is not required. Check DNS, TLS trust (for HTTPS), and signed
requests from the actual runner, not from a laptop or another CS cluster.
Never disable certificate validation to make an internal endpoint work.

Provision `ListBucket` permission scoped to the authorized test prefix, object
read/write/delete permissions only below that prefix, and bucket-location
lookup if required by the SDK. Do not grant bucket deletion. The tests do not
create buckets or empty a bucket. Bucket versioning may retain previous object
versions after ordinary deletion; apply a separately reviewed lifecycle policy
to the dedicated test scope if versioning is enabled.

The MatrixOne test changes in matrixorigin/matrixone#32590 must be merged
first. Each workflow attempt uses `ci-ut/<run-id>-<attempt>/` and the tests
create random child prefixes. A fixture refuses to run if its child prefix is
not empty; normal Go test cleanup deletes only keys beneath that child prefix.
Abrupt process or runner termination can leave objects behind; any lifecycle
policy or manual cleanup must be restricted to the dedicated test scope.

This mode intentionally does not exercise real Tencent COS or Aliyun OSS
provider integrations. Their absence is an explicitly accepted scope change,
not a rollout blocker. This PR does not claim equivalent vendor-specific
coverage or require these integrations to run in the UT job.

Existing TKE skipped tests are accepted unchanged. Migration validation checks
for new skipped test identities, failures and package/suite timeouts, not equal
skip totals. Successful and failed runs both upload
`ut-coverage-skip-manifest.json`: source SHA, actual runner, test/package skip
distinction, reasons, failed tests and package timeout events. Dynamic cloud
cases that are not generated are explicitly outside this comparison.

For a controlled comparison, use the same source SHA and package selection.
`canary_skip_baseline` is restricted to the same approved PR #32590 commit and
test branch as the JED canary, on the original TKE runner label, with no real
COS/OSS or CS credentials. It establishes non-storage skip behavior; it is not
a substitute for each CS environment's mandatory real S3 acceptance. Compare
the downloaded manifests with:

```sh
python3 scripts/coverage_skip_manifest.py --baseline tke.json --candidate cs.json --output comparison.json
```

The comparison rejects different source/package scopes, failed producers,
package timeouts, malformed evidence and newly skipped test identities.

Before rollout, run the isolated MinIO smoke (including an unrelated sentinel
object and failure-path cleanup tests), then a real S3 smoke from each runner
environment with the approved credentials, and finally PR Coverage. Keep the
CI PR in draft until configuration and end-to-end verification are complete.
Roll out all configured environments together; do not silently reduce the
production default to JED while other shared-pool configurations are missing.

For pre-merge verification, `canary_cs_s3` is restricted to a manual call
from `matrixorigin/matrixone:weilu-runner-test`. Its
`canary_cs_s3_environment` must be one of `mty`, `mnl2`, `jed`, or `ruh`, and
the runner label must exactly match `cs-<environment>-large-7c14g`. It
checks PR #32590's owner, repository and exact approved head SHA before using
the matching environment's restricted configuration. The normal shared-pool gate still requires all four
environment configurations; the canary does not weaken normal PR eligibility.
This runs the existing all-package Coverage selection, not just the S3 smoke,
and must observe successful CS S3 basic and streaming-write test events. Real
COS/OSS provider tests remain explicitly outside this CS validation.

Provisioned dedicated scopes (do not substitute business buckets):

| Environment | Dedicated bucket | Test prefix |
| --- | --- | --- |
| MTY | `217855-mo-ci-ut` | `ci-ut/<run-id>-<attempt>/` |
| MNL2 | `361800-mo-ci-ut` | `ci-ut/<run-id>-<attempt>/` |
| JED | `mo-ci-ut` | `ci-ut/<run-id>-<attempt>/` |
| RUH | `589004-mo-ci-ut` | `ci-ut/<run-id>-<attempt>/` |

The configuration secrets contain only restricted service-account credentials,
not the parent provisioning credentials. Verify signed read/write/list and
cleanup from the actual matching runner and deny business-bucket access before
enabling each environment. Provisioning success alone is not UT validation.
