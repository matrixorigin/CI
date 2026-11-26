#!/usr/bin/env bash
set -euo pipefail

packages=()
need_package() {
    local package=$1 tool missing=false
    shift
    for tool in "$@"; do
        if ! command -v "$tool" >/dev/null; then
            printf 'Missing build command: %s (package: %s)\n' "$tool" "$package"
            missing=true
        fi
    done
    if [[ "$missing" == true ]]; then
        packages+=("$package")
    fi
}

need_package build-essential gcc g++ make
need_package pkg-config pkg-config
need_package cmake cmake
need_package python3 python3

if (( ${#packages[@]} )); then
    # Keep signature/hash checks. Retry transient index sync errors, but fail
    # before using an incomplete index if all attempts are unsuccessful.
    apt=(sudo -n apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=1
         -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)
    for attempt in 1 2 3; do
        if "${apt[@]}" -o APT::Update::Error-Mode=any update; then
            break
        fi
        if (( attempt == 3 )); then
            echo "::error::Ubuntu build dependency indexes are unavailable after three attempts"
            exit 1
        fi
        echo "::warning::Ubuntu index refresh failed (attempt $attempt of 3); retrying"
        sleep "$((attempt * 5))"
    done
    "${apt[@]}" install -y --no-install-recommends "${packages[@]}"
else
    echo 'Build dependencies are already available.'
fi

for tool in gcc g++ make pkg-config cmake python3; do
    command -v "$tool" || { echo "::error::Missing build command after setup: $tool"; exit 1; }
done

# Existing coverage helpers invoke python. Use the already verified Python 3
# for this job only; do not depend on the distro's optional command alias.
python_bin=$(mktemp -d "$RUNNER_TEMP/coverage-python.XXXXXXXX")
ln -s "$(command -v python3)" "$python_bin/python"
printf '%s\n' "$python_bin" >> "$GITHUB_PATH"
