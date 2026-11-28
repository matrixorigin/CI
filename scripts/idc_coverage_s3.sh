# Sourced by the Shanghai Coverage workers from the trusted CI revision.
configure_idc_coverage_s3() {
  local scope="$1"
  TEST_S3FS_CS="$(python3 - <<'PY'
import os

fields = [(k, os.environ.get(v, "")) for k, v in (
    ("endpoint", "IDC_ENDPOINT"), ("region", "IDC_REGION"), ("bucket", "IDC_BUCKET"),
    ("key-id", "IDC_KEY"), ("key-secret", "IDC_SECRET"),
)]
if any(not value or any(c in value for c in ",\r\n") for _, value in fields):
    raise SystemExit("Missing or unsupported IDC S3 configuration")
print("name=cs,is-minio=true," + ",".join(key + "=" + value for key, value in fields))
PY
  )" || return
  export TEST_S3FS_CS
  export MO_CI_S3_PREFIX="ci-ut/${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}/${scope}"
  unset IDC_ENDPOINT IDC_REGION IDC_BUCKET IDC_KEY IDC_SECRET
  unset endpoint region apikey apisecret bucket TEST_S3FS_ALIYUN TEST_S3FS_QCLOUD
}

require_idc_coverage_s3_tests() {
  local report="$1" required
  for required in 'TestObjectStorages/cs-s3/minio/basic' 'TestS3FSFromSpecs/cs-s3/streaming_write'; do
    jq -e --arg test_name "$required" 'select(.Action == "pass" and .Package == "github.com/matrixorigin/matrixone/pkg/fileservice" and .Test == $test_name)' "$report" >/dev/null || {
      echo "::error::Required IDC S3 test did not pass: ${required}. If the fixture is absent, update the PR branch from its base."
      return 1
    }
  done
}
