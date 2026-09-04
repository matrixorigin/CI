#!/usr/bin/env bash

# Normalize the cache action's raw output and classify the restored contents.
# Output fields are tab-delimited: exact_match, usable_exact_hit, state.
set -euo pipefail

raw_exact_match="${1:-}"
usable="${2:-}"
matched_key="${3:-}"

exact_match="false"
if [[ "$raw_exact_match" == "true" ]]; then
  exact_match="true"
fi

cache_hit="false"
if [[ "$exact_match" == "true" ]] && [[ "$usable" == "true" ]]; then
  cache_hit="true"
  state="exact hit"
elif [[ "$usable" == "true" ]]; then
  state="prefix hit"
elif [[ -n "$matched_key" ]]; then
  state="unusable restore"
else
  state="miss"
fi

printf '%s\t%s\t%s\n' "$exact_match" "$cache_hit" "$state"
