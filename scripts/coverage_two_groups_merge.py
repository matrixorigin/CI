#!/usr/bin/env python3
"""Validate complete group evidence before merging coverage profiles."""
import argparse
import json
from pathlib import Path


def validate(root):
    plans = [json.loads((root / str(i) / 'coverage-group-plan.json').read_text()) for i in range(3)]
    results = [json.loads((root / str(i) / 'ut-coverage-skip-manifest.json').read_text()) for i in range(3)]
    for i in range(3):
        p, r = plans[i], results[i]
        if p['selected_group'] != i or r['producer_status'] != 'passed':
            raise ValueError('group identity or producer status mismatch')
        if r['malformed_events'] or r['failed'] or r['package_timeouts']:
            raise ValueError('group reported failed tests, malformed events, or timeout')
        if set(r['started_packages']) != set(p['groups'][i]):
            raise ValueError('actual package scope does not match group plan')
    if any(plans[0][key] != plan[key] for plan in plans[1:] for key in ('scope_sha256', 'scope_count', 'groups')):
        raise ValueError('group plans differ')
    groups = list(map(set, plans[0]['groups']))
    if len(groups) != 3 or not all(groups) or sum(map(len, groups)) != len(set().union(*groups)) or len(set().union(*groups)) != plans[0]['scope_count']:
        raise ValueError('incomplete or overlapping group scope')
    if any(results[0]['source_sha'] != result['source_sha'] for result in results[1:]):
        raise ValueError('source mismatch')
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    results = validate(args.root)
    result = dict(results[0])
    for key in ('started_packages', 'skipped_tests', 'packages_without_tests', 'failed', 'package_timeouts'):
        result[key] = sum((item[key] for item in results), [])
    result['started_packages'] = sorted(result['started_packages'])
    result['runner'] = 'three isolated runner jobs'
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"Validated {len(result['started_packages'])} packages across all groups")


if __name__ == '__main__':
    main()
