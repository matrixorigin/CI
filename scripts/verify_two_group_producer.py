#!/usr/bin/env python3
"""Audit a two-group MTY producer against the accepted three-group baseline.

Partition identity is intentionally different; hardware/toolchain/source/native
compatibility is not. No old three-group cache is accepted or rewritten here.
"""
import hashlib
import json
from pathlib import Path
import sys

SOURCE = 'dbc7ca14ca8b01cfe6d9e9a151619ea4b5ba14ca'


def read(path):
    return json.loads(path.read_text())


def profile(path):
    with path.open() as f:
        assert next(f).strip() == 'mode: set'
        result = set()
        for line in f:
            fields = line.split()
            assert len(fields) == 3 and fields[1].isdigit() and fields[2] in ('0', '1')
            block = tuple(fields[:2])
            assert block not in result
            result.add(block)
    assert len(result) == 446657
    return result


def verify(root):
    base = read(root/'baseline/skips/combined-skip-manifest.json')
    candidate = read(root/'candidate/skips/combined-skip-manifest.json')
    for m in (base, candidate):
        assert m['source_sha'] == SOURCE and m['storage_mode'] == 'cs-s3'
        assert len(m['started_packages']) == len(set(m['started_packages'])) == 505
        assert not m['failed'] and not m['package_timeouts'] and not m['malformed_events']
        assert len(m['skipped_tests']) == 86
    assert set(base['started_packages']) == set(candidate['started_packages'])
    key = lambda t: (t['package'], t['test'])
    assert sorted(base['skipped_tests'], key=key) == sorted(candidate['skipped_tests'], key=key)
    assert sorted(base['packages_without_tests']) == sorted(candidate['packages_without_tests'])
    assert profile(root/'baseline/profile/ut-coverage.out') == profile(root/'candidate/profile/ut-coverage.out')
    reports = sorted((root/'candidate/reports').glob('cold-ut-coverage-report-group-*-attempt-*'))
    assert len(reports) == 2
    expected_policy = hashlib.sha256(Path(__file__).with_name('coverage_build_cache.py').read_bytes()).hexdigest()
    expected_partition = hashlib.sha256(Path(__file__).with_name('coverage_two_groups.py').read_bytes()).hexdigest()
    packages = []
    for g, folder in enumerate(reports):
        assert folder.name.startswith(f'cold-ut-coverage-report-group-{g}-attempt-')
        m = read(folder/'build-cache-artifact/manifest.json')
        fp = m['fingerprint']
        assert m['compile_only'] is True and fp['source'] == SOURCE and fp['cs_environment'] == 'mty'
        assert fp['group'] == str(g) and fp['group_count'] == 2
        assert fp['policy'] == expected_policy and fp['partition_policy'] == expected_partition
        baseline = read(root/'baseline/reports'/f'baseline-ut-coverage-report-group-{g}-attempt-1/baseline-environment.json')
        assert baseline['restored_cache'] is False and baseline['seed_precompile'] is False
        # This is the deliberate 3 -> 2 partition/policy change. Retain every
        # other key, including environment, CPU, tools and absolute paths.
        old = {k: v for k, v in baseline['fingerprint'].items() if k not in ('run','attempt','policy')}
        new = {k: v for k, v in fp.items() if k not in ('run','attempt','policy','group_count','partition_policy')}
        assert old == new
        contract = m['coverage_contract']
        assert set(contract['full_packages']) == set(base['started_packages'])
        assert len(contract['coverpkg']) == len(set(contract['coverpkg'])) == 484
        from coverage_two_groups import plan
        assert contract['packages'] == plan(contract['full_packages'], 2)['groups'][g]
        packages += contract['packages']
    assert len(packages) == len(set(packages)) == 505 and set(packages) == set(base['started_packages'])
    return {'source': SOURCE, 'environment': 'mty', 'groups': 2, 'packages': 505,
            'instrumented': 484, 'skips': 86, 'profile_blocks': 446657,
            'same_scope': True, 'partition_change_explicit': True, 'pvc_reuse_validated': False}


if __name__ == '__main__':
    root = Path(sys.argv[1])
    result = verify(root)
    (root/'acceptance.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))
