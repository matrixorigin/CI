#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

assert_state() {
  local expected="$1"
  shift
  local actual
  actual="$(bash "$script_dir/cache-state.sh" "$@")"
  if [[ "$actual" != "$expected" ]]; then
    printf 'expected <%s>, got <%s>\n' "$expected" "$actual" >&2
    return 1
  fi
}

assert_state $'true\ttrue\texact hit' true true exact-key
assert_state $'false\tfalse\tprefix hit' false true prefix-key
assert_state $'true\tfalse\tunusable restore' true false exact-key
assert_state $'false\tfalse\tunusable restore' false false prefix-key
assert_state $'false\tfalse\tmiss' false false ''

echo 'cache state tests passed'
