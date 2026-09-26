#!/usr/bin/env python3
"""Preserve CN shutdown evidence before Compose removes the containers."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


CN_SERVICES = ("cn-0", "cn-1")
SQL_EXECUTOR = "/pkg/frontend/mysql_cmd_executor.go:"
BLOCK = re.compile(r"\S+:\d+\.\d+,\d+\.\d+ \d+ (\d+)$")


def run(args, timeout=30):
    return subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE, timeout=timeout
    ).stdout


def supports_coverage(compose_file):
    config = compose_file.read_text()
    return "GOCOVERDIR=/coverage" in config and "coverage:/coverage" in config


def stop_cns(compose_file):
    if not supports_coverage(compose_file):
        print("::warning::Skipping CN coverage checks for this legacy Compose configuration")
        return
    compose = ["docker", "compose", "-f", str(compose_file),
               "--profile", "launch-multi-cn"]
    containers = {}
    for service in CN_SERVICES:
        ids = run(compose + ["ps", "--all", "--quiet", service]).split()
        if len(ids) != 1:
            raise ValueError(f"expected one {service} container, found {len(ids)}")
        containers[service] = ids[0]

    # CN shutdown allows one minute for draining, plus shutdown profiles.
    # Keep TN/log dependencies alive and allow more than Docker's default 10s.
    # A hung shutdown still fails the exit-state check below.
    print(run(compose + ["stop", "--timeout", "120", *CN_SERVICES], timeout=180), end="")
    failures = []
    for service, container in containers.items():
        state = json.loads(run(["docker", "inspect", "--format", "{{json .State}}", container]))
        print(f"{service} shutdown: {json.dumps(state, sort_keys=True)}", flush=True)
        if (state.get("Status") != "exited" or state.get("ExitCode") != 0
                or state.get("OOMKilled") is not False or state.get("Error")):
            failures.append(service)
    if failures:
        raise ValueError(f"CN shutdown did not complete cleanly: {', '.join(failures)}; "
                         "coverage may be incomplete")


def validate_profile(profile):
    sql_executed = False
    with profile.open() as stream:
        if stream.readline().strip() not in {"mode: set", "mode: count", "mode: atomic"}:
            raise ValueError("invalid coverage profile mode")
        for line in stream:
            block = BLOCK.fullmatch(line.strip())
            if block is None:
                raise ValueError("invalid coverage profile block")
            if SQL_EXECUTOR in line and int(block.group(1)) > 0:
                sql_executed = True
    if not sql_executed:
        raise ValueError("BVT coverage has no SQL executor hits; CN counters are missing")


def generate_profile(compose_file, coverage_dir, output):
    output.unlink(missing_ok=True)
    if not supports_coverage(compose_file):
        print("::warning::BVT coverage is unsupported by this legacy Compose configuration")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="bvt-coverage-") as directory:
        temporary = Path(directory) / "profile.out"
        run(["go", "tool", "covdata", "textfmt", f"-i={coverage_dir}", f"-o={temporary}"], timeout=180)
        validate_profile(temporary)
        temporary.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stop", "profile"))
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--coverage-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.operation == "stop":
        stop_cns(args.compose_file)
    else:
        if args.coverage_dir is None or args.output is None:
            parser.error("profile requires --coverage-dir and --output")
        generate_profile(args.compose_file, args.coverage_dir, args.output)


if __name__ == "__main__":
    main()
