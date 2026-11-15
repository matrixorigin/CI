#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
hash_script="$script_dir/environment-hash.sh"
base=(go1.26.4 cc-1 cxx-1 cmake-1 tool-1 /work/matrixone /work/matrixone/.sca-go-module-cache /go /go-build)

base_hash="$(bash "$hash_script" "${base[@]}")"
test -n "$base_hash"
test "$base_hash" = "$(bash "$hash_script" "${base[@]}")"

# Every compatibility input must own an independent cache lineage, especially
# paths whose absolute spelling is embedded in golangci-lint package hashes.
for index in "${!base[@]}"; do
  changed=("${base[@]}")
  changed[$index]="${changed[$index]}-changed"
  test "$base_hash" != "$(bash "$hash_script" "${changed[@]}")"
done

left=(a bc c d e f g h i)
right=(ab c c d e f g h i)
test "$(bash "$hash_script" "${left[@]}")" != "$(bash "$hash_script" "${right[@]}")"

if bash "$hash_script" too few inputs >/dev/null 2>&1; then
  echo "environment hash accepted an incomplete compatibility identity" >&2
  exit 1
fi

echo "environment hash tests passed"
