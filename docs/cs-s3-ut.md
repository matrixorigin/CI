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

The MatrixOne test changes in matrixorigin/matrixone#32590 must be merged
first. Each workflow attempt uses `ci-ut/<run-id>-<attempt>/` and the tests
create random child prefixes. A fixture refuses to run if its child prefix is
not empty; normal Go test cleanup deletes only keys beneath that child prefix.
Abrupt process or runner termination can leave objects behind; any lifecycle
policy or manual cleanup must be restricted to the dedicated test scope.

This mode intentionally does not exercise real Tencent COS or Aliyun OSS
provider integrations. Retain or establish separate daily coverage for those
providers before claiming equivalent vendor-specific coverage.
