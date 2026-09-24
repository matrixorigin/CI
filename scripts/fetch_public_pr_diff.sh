#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "usage: GH_TOKEN=... $0 <repository> <pull-number> <expected-head-sha> <output-path>" >&2
  exit 2
}

[[ $# -eq 4 ]] || usage

pr_repo=$1
pr_number=$2
expected_head_sha=$3
output_path=$4
: "${GH_TOKEN:?GH_TOKEN is required for the authenticated API fallback}"

[[ "$pr_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
[[ "$pr_number" =~ ^[1-9][0-9]*$ ]] || usage
[[ "$expected_head_sha" =~ ^[[:xdigit:]]{40}$ ]] || usage

output_dir=$(dirname -- "$output_path")
mkdir -p -- "$output_dir"
# Never let a failed fetch leave a previous diff that the parser could consume.
rm -f -- "$output_path"
temp_dir=$(mktemp -d "${output_dir}/.fetch-pr-diff.XXXXXX")
trap 'rm -rf -- "$temp_dir"' EXIT

api_pr_url="https://api.github.com/repos/${pr_repo}/pulls/${pr_number}"
candidate_patch="${temp_dir}/candidate.patch"
pr_metadata="${temp_dir}/pull-request.json"

curl_with_retry=(
  --silent --show-error --fail --location
  --connect-timeout 5 --max-time 15
  --retry 4 --retry-delay 3 --retry-max-time 45 --retry-connrefused
)

request() {
  local source=$1
  local response_path=$2
  shift 2

  local http_status=""
  local curl_status=0
  http_status=$(curl "${curl_with_retry[@]}" "$@" \
    --output "$response_path" --write-out '%{http_code}') || curl_status=$?
  if ((curl_status != 0)); then
    echo "${source} request failed (curl exit ${curl_status}, HTTP ${http_status:-unknown})" >&2
    return 1
  fi
  if [[ "$http_status" != "200" ]]; then
    echo "${source} request returned unexpected HTTP ${http_status:-unknown}" >&2
    return 1
  fi
}

current_pr_head() {
  request "GitHub PR metadata" "$pr_metadata" \
    -H 'Accept: application/vnd.github+json' \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "$api_pr_url" || return 1

  local head_sha
  head_sha=$(jq -er '.head.sha | select(type == "string")' "$pr_metadata") || {
    echo "GitHub PR metadata did not contain a head SHA" >&2
    return 1
  }
  if [[ ! "$head_sha" =~ ^[[:xdigit:]]{40}$ ]]; then
    echo "GitHub PR metadata returned an invalid head SHA" >&2
    return 1
  fi
  printf '%s' "$head_sha"
}

verify_pr_head() {
  local phase=$1
  local current_head_sha
  current_head_sha=$(current_pr_head) || return 1
  if [[ "$current_head_sha" != "$expected_head_sha" ]]; then
    echo "PR head moved ${phase}: expected ${expected_head_sha}, found ${current_head_sha}; refusing to publish coverage diff" >&2
    return 1
  fi
}

pr_changed_files() {
  local changed_files
  changed_files=$(jq -er '.changed_files | select(type == "number" and . >= 0 and . == floor)' "$pr_metadata") || {
    echo "GitHub PR metadata did not contain a valid changed-files count" >&2
    return 1
  }
  printf '%s' "$changed_files"
}

validate_diff() {
  local source=$1
  local first_line
  if [[ ! -s "$candidate_patch" ]]; then
    if [[ "$expected_changed_files" != "0" ]]; then
      echo "${source} returned an empty diff despite changed_files=${expected_changed_files}" >&2
      return 1
    fi
    return 0
  fi
  if [[ "$expected_changed_files" == "0" ]]; then
    echo "${source} returned a non-empty diff despite changed_files=0" >&2
    return 1
  fi

  IFS= read -r first_line < "$candidate_patch" || true
  if [[ "$first_line" != 'diff --git '* ]]; then
    echo "${source} returned a non-empty response that is not a Git diff; refusing to treat it as no changed lines" >&2
    return 1
  fi

  local diff_file_count
  if ! diff_file_count=$(git apply --numstat -- "$candidate_patch" | awk 'END { print NR }'); then
    echo "${source} returned a malformed Git diff" >&2
    return 1
  fi
  if [[ "$diff_file_count" != "$expected_changed_files" ]]; then
    echo "${source} diff has file_count=${diff_file_count} despite changed_files=${expected_changed_files}" >&2
    return 1
  fi
}

download_diff() {
  local source=$1
  shift
  : > "$candidate_patch"
  if ! request "$source" "$candidate_patch" "$@"; then
    return 1
  fi
  validate_diff "$source"
}

verify_pr_head "before diff download"
expected_changed_files=$(pr_changed_files)

public_diff_url="https://github.com/${pr_repo}/pull/${pr_number}.diff"
if download_diff "public PR diff" "$public_diff_url"; then
  echo "Downloaded PR diff from public URL"
else
  echo "Public PR diff failed; falling back to authenticated GitHub API" >&2
  if ! download_diff "GitHub API PR diff" \
    -H 'Accept: application/vnd.github.diff' \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "$api_pr_url"; then
    echo "Unable to download a valid PR diff from either GitHub endpoint" >&2
    exit 1
  fi
  echo "Downloaded PR diff from authenticated GitHub API"
fi

# The PR can be updated while the diff endpoints are retrying. Publish only
# when both metadata checks agree on the expected head and changed-file count.
verify_pr_head "after diff download"
current_changed_files=$(pr_changed_files)
if [[ "$current_changed_files" != "$expected_changed_files" ]]; then
  echo "PR changed-file count moved during diff download: expected ${expected_changed_files}, found ${current_changed_files}; refusing to publish coverage diff" >&2
  exit 1
fi
mv -- "$candidate_patch" "$output_path"
