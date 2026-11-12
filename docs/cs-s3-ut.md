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
provider integrations. Retain or establish separate daily coverage for those
providers before claiming equivalent vendor-specific coverage.

Before rollout, run the isolated MinIO smoke (including an unrelated sentinel
object and failure-path cleanup tests), then a real S3 smoke from each runner
environment with the approved credentials, and finally PR Coverage. Keep the
CI PR in draft until configuration and end-to-end verification are complete.
