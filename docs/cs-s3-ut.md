# CS S3 for UT Coverage

Coverage runs automatically on `cs-large-7c14g`. The entire job, including all
eight parallel Go packages and S3 tests, stays on the selected runner. Another
PR/job may land in another environment and selects its own S3 configuration.

Configure all four secrets in the caller repository's `ci` environment:

| Runner name prefix | Secret | Dedicated CI bucket |
| --- | --- | --- |
| `cs-mty-` | `CS_S3_MTY_CONFIG` | `217855-mo-ci-ut` |
| `cs-mnl2-` | `CS_S3_MNL2_CONFIG` | `361800-mo-ci-ut` |
| `cs-jed-` | `CS_S3_JED_CONFIG` | `mo-ci-ut` |
| `cs-ruh-` | `CS_S3_RUH_CONFIG` | `589004-mo-ci-ut` |

Each secret is a CSV-style specification (never commit real credentials):

```
name=cs,endpoint=https://s3.example.invalid,region=us-east-1,bucket=ci-test,key-id=ACCESS_KEY,key-secret=SECRET_KEY,is-minio=true
```

Unknown runners or missing matching configuration fail; storage never falls
back to another environment. JED may use its cluster-internal S3 endpoint.
Use restricted test accounts: prefix-scoped list/read/write/delete and bucket
location lookup if needed, never business-bucket access or bucket deletion.

Requires the isolated fixtures in [MatrixOne #32590](https://github.com/matrixorigin/matrixone/pull/32590).
Each attempt uses `ci-ut/<run-id>-<attempt>/` with random fixture subdirectories.
Fixtures refuse nonempty prefixes and clean only their own objects. Interrupted
runs may leave test objects; any lifecycle policy must stay within `ci-ut/`.
No local MinIO is started. Real COS/OSS integrations are intentionally excluded.
The CS S3 basic and streaming-write tests must actually pass, not skip.

Success and failure both upload a redacted `ut-coverage-skip-manifest.json`.
For migration acceptance, compare same-source, same-package-scope manifests:

```sh
python3 scripts/coverage_skip_manifest.py --baseline tke.json --candidate cs.json --output comparison.json
```

The check rejects new skipped test identities, failed/timed-out producers,
malformed evidence and mismatched source/package scopes; existing skips are
accepted. It is an explicit artifact comparison, not an automatic cross-run
baseline lookup. Real COS/OSS cases excluded from generation are outside it.
Merge the MO fixtures first; validate all four environments before rollout.
