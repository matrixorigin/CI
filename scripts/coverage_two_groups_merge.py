#!/usr/bin/env python3
"""Validate complete group evidence before merging coverage profiles."""
import argparse
import json
from pathlib import Path


def validate(root):
    plans = [json.loads((root / str(i) / 'coverage-group-plan.json').read_text()) for i in range(2)]
    results = [json.loads((root / str(i) / 'ut-coverage-skip-manifest.json').read_text()) for i in range(2)]
    for i in range(2):
        p, r = plans[i], results[i]
        if p['selected_group'] != i or r['producer_status'] != 'passed':
            raise ValueError('group identity or producer status mismatch')
        if r['malformed_events'] or r['failed'] or r['package_timeouts']:
            raise ValueError('group reported failed tests, malformed events, or timeout')
        if set(r['started_packages']) != set(p['groups'][i]):
            raise ValueError('actual package scope does not match group plan')
    if any(plans[0][key] != plans[1][key] for key in ('scope_sha256', 'scope_count', 'groups')):
        raise ValueError('group plans differ')
    a, b = map(set, plans[0]['groups'])
    if not a or not b or a & b or len(a | b) != plans[0]['scope_count']:
        raise ValueError('incomplete or overlapping group scope')
    if results[0]['source_sha'] != results[1]['source_sha']:
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
        result[key] = results[0][key] + results[1][key]
    result['started_packages'] = sorted(result['started_packages'])
    result['runner'] = 'two isolated runner jobs'
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"Validated {len(result['started_packages'])} packages across both groups")


if __name__ == '__main__':
    main()
