#!/usr/bin/env bash

# Build the compatibility identity for an analysis-cache archive. The last
# four inputs are absolute paths embedded by go/packages and golangci-lint in
# package action IDs, so omitting any of them can turn an outer exact hit into
# an internal cache miss.
set -euo pipefail

if [[ "$#" -ne 9 ]]; then
  echo "usage: environment-hash.sh GO_VERSION CC CXX CMAKE TOOL_RECIPE MODULE_ROOT GOMODCACHE GOROOT GOCACHE" >&2
  exit 2
fi

# NUL separators make the identity unambiguous even when adjacent values have
# different boundaries but the same concatenation.
printf '%s\0' "$@" | sha256sum | awk '{print $1}'
