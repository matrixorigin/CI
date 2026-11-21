#!/usr/bin/env python3
"""Deterministically partition a complete Go package list into three groups.

Weights are approximate elapsed seconds from one MNL2 Coverage run; they are
only scheduling hints, never an allowlist. Unknown packages are still tested.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = 'github.com/matrixorigin/matrixone/'
WEIGHTS = {
    'pkg/tests/sqlintegration': 879, 'pkg/tests/service': 638,
    'pkg/tests/issues/isolated': 633, 'pkg/embed': 511,
    'pkg/tests/issues': 474, 'pkg/sql/plan': 219,
    'pkg/tests/sqlintegration/multicn': 208, 'pkg/vm/engine/test': 189,
    'pkg/tests/arrowload': 142, 'pkg/logservice': 127,
    'pkg/tests/upgrade': 117, 'pkg/tests/partition': 101,
    'pkg/tests/dml': 94, 'pkg/vm/engine/tae/db/test': 83,
    'optools': 82, 'pkg/shardservice': 57,
    'pkg/vm/engine/disttae': 54, 'pkg/tests/shard': 54,
    'pkg/lockservice': 50, 'pkg/frontend': 46,
}
FILE_SERVICE = ROOT + 'pkg/fileservice'
EMBED = ROOT + 'pkg/embed'


def plan(packages):
    if not packages or len(set(packages)) != len(packages):
        raise ValueError('empty or duplicate package scope')
    if FILE_SERVICE not in packages or EMBED not in packages:
        raise ValueError('required S3 or embed test package is missing')
    # Seed heavy packages into different groups; shared-pool jobs can land in
    # any environment, so no group is assumed to use a particular runner.
    groups = [[FILE_SERVICE], [], [EMBED]]
    weights = [5, 0, WEIGHTS['pkg/embed']]
    for package in sorted(set(packages) - {FILE_SERVICE, EMBED},
                          key=lambda p: (-WEIGHTS.get(p.removeprefix(ROOT), 5), p)):
        index = min(range(3), key=lambda i: (weights[i], i))
        groups[index].append(package)
        weights[index] += WEIGHTS.get(package.removeprefix(ROOT), 5)
    groups = [sorted(group) for group in groups]
    if not all(groups) or sum(len(set(g)) for g in groups) != len(set().union(*map(set, groups))) or set().union(*map(set, groups)) != set(packages):
        raise ValueError('invalid partition')
    scope = sorted(packages)
    digest = hashlib.sha256(('\n'.join(scope) + '\n').encode()).hexdigest()
    return {'schema': 1, 'scope_sha256': digest, 'scope_count': len(scope),
            'groups': groups, 'estimated_weight': weights}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--packages', required=True, type=Path)
    parser.add_argument('--group', required=True, type=int, choices=(0, 1, 2))
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--selected', required=True, type=Path)
    args = parser.parse_args()
    result = plan(args.packages.read_text().splitlines())
    result['selected_group'] = args.group
    args.manifest.write_text(json.dumps(result, sort_keys=True) + '\n')
    args.selected.write_text('\n'.join(result['groups'][args.group]) + '\n')
    print(f"group={args.group} selected={len(result['groups'][args.group])} "
          f"scope={result['scope_count']} scope_sha256={result['scope_sha256']}")


if __name__ == '__main__':
    main()
