#!/usr/bin/env python3
"""Keep embed's package deadline independent of other cluster-owner queues.

All selected packages execute once with the same instrumentation and deadlines.
The outer run_coverage_ut.py retains the whole-suite deadline/process group.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


EMBED = "github.com/matrixorigin/matrixone/pkg/embed"


def groups(packages):
    if not packages or len(packages) != len(set(packages)):
        raise ValueError("coverage package scope must be nonempty and unique")
    isolated = [package for package in packages if package == EMBED]
    remainder = [package for package in packages if package != EMBED]
    return [group for group in (isolated, remainder) if group]


def run(packages, profile, coverpkg, parallel, execute=subprocess.run):
    plan = groups(packages)
    if profile.exists():
        raise ValueError("refusing to overwrite an existing coverage profile")
    status = 0
    with tempfile.TemporaryDirectory(prefix="coverage-groups-", dir=profile.parent) as directory:
        parts = []
        for index, group in enumerate(plan):
            part = Path(directory) / f"{index}.out"
            command = ["go", "test", "-mod=readonly", "-count=1", "-json", "-short", "-v",
                       "-tags", "matrixone_test", "-timeout=20m", "-p", str(parallel),
                       "-covermode=set", f"-coverprofile={part}", f"-coverpkg={coverpkg}", *group]
            # Inherit the outer process group so its bounded timeout also
            # terminates this scheduler, go test and all test descendants.
            result = execute(command)
            if result.returncode:
                status = 1
            parts.append(part)
        with profile.open("xb") as output:
            output.write(b"mode: set\n")
            for part in parts:
                if not part.is_file():
                    status = 1
                    continue
                with part.open("rb") as source:
                    if source.readline() != b"mode: set\n":
                        status = 1
                        continue
                    shutil.copyfileobj(source, output)
        # The existing compact_coverage_profile helper ORs duplicate set-mode
        # blocks after this returns. No source/package is dropped here.
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--coverpkg", required=True)
    parser.add_argument("--parallel", required=True, type=int)
    parser.add_argument("packages", nargs="+")
    args = parser.parse_args()
    if args.parallel < 1:
        parser.error("parallelism must be positive")
    return run(args.packages, args.profile, args.coverpkg, args.parallel)


if __name__ == "__main__":
    raise SystemExit(main())
