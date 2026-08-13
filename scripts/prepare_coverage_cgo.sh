#!/usr/bin/env bash

set -euo pipefail

make clean
make config
make cgo

for header in \
    thirdparties/install/include/xxhash.h \
    thirdparties/install/include/roaring.h \
    thirdparties/install/include/usearch.h; do
    if [[ ! -f "${header}" ]]; then
        echo "missing required CGO header: ${header}" >&2
        exit 1
    fi
done
