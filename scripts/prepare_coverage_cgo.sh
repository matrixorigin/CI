#!/usr/bin/env bash

set -euo pipefail

make clean
make config

# Seed prebuilt C thirdparties from the CI builder image when the caller
# provides them (make clean just wiped any previous copy). The file targets
# in thirdparties/Makefile then treat the libraries as up to date, so make
# cgo below only rebuilds MatrixOne's own C code.
if [[ -n "${MO_PREBUILT_THIRDPARTIES:-}" && -d "${MO_PREBUILT_THIRDPARTIES}" ]]; then
    rm -rf thirdparties/install
    cp -r "${MO_PREBUILT_THIRDPARTIES}" thirdparties/install
fi

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
