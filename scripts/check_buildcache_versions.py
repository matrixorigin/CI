#!/usr/bin/env python3
"""Fail closed on the immutable revisions used by this isolated experiment.

This deliberately checks the committed arm/helper, not their working-tree
counterparts. Parsing is limited to this workflow's literal pinned fields;
missing/duplicate/dynamic fields require a checker update, never a fallback.
"""
import argparse
import ast
from pathlib import Path
import re
import subprocess

HEX = r'[0-9a-f]{40}'
ARM = '.github/workflows/coverage-buildcache-arm.yaml'


def require(value, message):
    if not value:
        raise ValueError(message)


def one(pattern, text, label):
    values = re.findall(pattern, text, re.MULTILINE)
    require(len(values) == 1, 'missing or duplicate ' + label)
    return values[0]


def source_constant(text):
    nodes = [n for n in ast.parse(text).body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'SOURCE' for t in n.targets)]
    require(len(nodes) == 1, 'missing or duplicate helper SOURCE')
    value = ast.literal_eval(nodes[0].value)
    require(isinstance(value, str) and re.fullmatch(HEX, value), 'nonliteral source SHA')
    return value


def verify_arm(read, arm_ref, expected):
    require(re.fullmatch(HEX, arm_ref) and re.fullmatch(HEX, expected), 'immutable SHA required')
    arm = read(arm_ref, ARM)
    checkout = one(r'repository: matrixorigin/matrixone\n\s+ref: (' + HEX + r')\s*$', arm, 'MO checkout')
    merge = one(r'^\s+EXPECTED_HEAD_SHA: (' + HEX + r')\s*$', arm, 'merge SHA')
    helper = one(r'- name: Checkout isolated build-cache helper\n[\s\S]*?repository: matrixorigin/CI\n\s+ref: (' + HEX + r')\s*$', arm, 'helper pin')
    actual = source_constant(read(helper, 'scripts/compile_cache_probe.py'))
    module = ast.parse(read(helper, 'scripts/coverage_build_cache.py'))
    imports = [n for n in module.body if isinstance(n, ast.ImportFrom)
               and n.module == 'compile_cache_probe'
               and any(a.name == 'SOURCE' and a.asname is None for a in n.names)]
    require(len(imports) == 1, 'full helper must use validated SOURCE')
    require(all(s == expected for s in (checkout, merge, actual)), 'source SHA mismatch across pinned revisions')
    return {'source': expected, 'arm': arm_ref, 'helper': helper}


def verify_pair(text, arm_ref, expected):
    arms = re.findall(r'uses: matrixorigin/CI/\.github/workflows/coverage-buildcache-arm.yaml@(' + HEX + r')\s*$', text, re.MULTILINE)
    require(arms == [arm_ref, arm_ref], 'cold and warm must use the validated arm')
    checkout = one(r'repository: matrixorigin/matrixone\n\s+ref: (' + HEX + r')\s*$', text, 'gate checkout')
    acceptance = one(r"assert m\['source_sha'\]=='(" + HEX + r")'", text, 'acceptance SHA')
    command = one(r'check_buildcache_versions.py --arm-ref (' + HEX + r') --expected-source (' + HEX + r')\s*$', text, 'hosted version guard')
    guard_checkout = one(r'- name: Checkout immutable version graph\n[\s\S]*?ref: (' + HEX + r')\s*$', text, 'version graph checkout')
    require(checkout == acceptance == expected and command == (arm_ref, expected)
            and guard_checkout == arm_ref, 'pair version mismatch')
    require('validate-cleanup:\n    needs: [validate-versions]' in text,
            'cleanup must depend on version validation')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arm-ref')
    parser.add_argument('--expected-source', required=True)
    parser.add_argument('--pair-file', type=Path)
    parser.add_argument('--source-dir', type=Path)
    args = parser.parse_args()
    require(re.fullmatch(HEX, args.expected_source), 'fixed source required')
    if args.source_dir:
        helper_source = source_constant(Path(__file__).with_name('compile_cache_probe.py').read_text())
        actual = subprocess.check_output(['git', '-C', str(args.source_dir), 'rev-parse', 'HEAD'], text=True).strip()
        require(helper_source == actual == args.expected_source, 'actual checkout/helper source mismatch')
        require(not subprocess.check_output(['git', '-C', str(args.source_dir), 'status', '--porcelain', '--untracked-files=no']), 'tracked source modified')
        print('Actual checkout and helper SOURCE verified:', actual)
    else:
        def read(ref, path):
            return subprocess.check_output(['git', 'show', ref + ':' + path], text=True)
        result = verify_arm(read, args.arm_ref, args.expected_source)
        if args.pair_file:
            verify_pair(args.pair_file.read_text(), args.arm_ref, args.expected_source)
        print('Immutable version graph verified:', result)


if __name__ == '__main__':
    main()
