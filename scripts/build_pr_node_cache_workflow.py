#!/usr/bin/env python3
"""Compose the opt-in PR consumer from the already validated execution path.

This does not change the existing workflow/caller or publish any cache. The new
common routing label must only be advertised by fully prepared node pools.
"""
import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = '.github/workflows/coverage-node-cache-pr.yaml'


def build():
    accepted = yaml.safe_load((ROOT / '.github/workflows/node-cache-local-tools-acceptance.yaml').read_text())
    original = yaml.safe_load((ROOT / '.github/workflows/coverage-ut.yaml').read_text())
    workflow = copy.deepcopy(accepted)
    workflow['name'] = 'PR UT Coverage - two groups with node-local read-only cache'
    call = workflow.pop('on', workflow.pop(True, None))['workflow_call']
    call['inputs'] = {k: copy.deepcopy(v) for k, v in call['inputs'].items()
                      if k in ('cache_seed', 'bvt_generation')}
    call['inputs']['cache_seed']['description'] = 'Compatibility only; normal PR consumers never seed or publish caches'
    workflow['on'] = {'workflow_call': call}
    gate = copy.deepcopy(original['jobs']['check_coverage_eligibility'])
    gate['if'] = "github.repository == 'matrixorigin/matrixone' && (github.event_name == 'pull_request' || github.event_name == 'pull_request_target')"
    gate['steps'] = [s for s in gate['steps'] if s.get('name') != 'Validate opt-in node-cache routing']
    gate['steps'].insert(0, {'name': 'Reject cache creation in the ordinary PR consumer',
        'env': {'CACHE_SEED': '${{ inputs.cache_seed }}'},
        'run': 'set -euo pipefail\ntest "$CACHE_SEED" = false\n'})
    workflow['jobs']['check_coverage_eligibility'] = gate
    job = workflow['jobs']['ut-coverage-linux-x86']
    job['strategy'] = {'fail-fast': False, 'max-parallel': 2, 'matrix': {'group': [0, 1]}}
    job['runs-on'] = 'cs-node-cache-7c14g'
    # No cross-PR concurrency key: independent PRs can consume all prepared slots.
    unwanted = {'reviewed_cache', 'download_reviewed_cache', 'restore_reviewed_cache'}
    job['steps'] = [s for s in job['steps'] if s.get('id') not in unwanted and
                    s.get('name') != 'Record reviewed cache outcome']
    for step in job['steps']:
        if step.get('name') == 'Checkout MatrixOne source':
            step['with']['repository'] = '${{ github.event.pull_request.head.repo.full_name }}'
            step['with']['ref'] = '${{ github.event.pull_request.head.sha }}'
        if step.get('with', {}).get('repository') == 'matrixorigin/CI':
            step['with']['repository'] = '${{ job.workflow_repository }}'
            step['with']['ref'] = '${{ job.workflow_sha }}'
        if step.get('name') == 'Checkout CI coverage helper':
            # New PR provenance/catalog verification imports the receipt's
            # complete transitive helper graph and reads its reviewed configs.
            # A legacy file allowlist silently omitted these runtime inputs.
            step['with']['sparse-checkout'] = 'scripts\nconfig\n'
            step['with']['sparse-checkout-cone-mode'] = True
        if step.get('id') == 'node_cache':
            # The trusted project namespace is not the fork provenance. Native
            # input hashes and Go ActionIDs still distinguish changed code.
            step['env']['CS_CACHE_SOURCE_REPOSITORY'] = 'matrixorigin/matrixone'
            step['env']['ACTUAL_PR_SOURCE_REPOSITORY'] = '${{ github.event.pull_request.head.repo.full_name }}'
            step['env']['ACTUAL_PR_HEAD_SHA'] = '${{ github.event.pull_request.head.sha }}'
            step['env'].pop('REQUESTED_CACHE_ENV', None)
            step['run'] = ('set -euo pipefail\ncase "${CS_CACHE_ENV:-}" in mty|mnl2|jed|ruh) ;; *) exit 1 ;; esac\n'
                'python3 .ci-workflow/scripts/node_cache_pr_source.py check\n'
                'python3 .ci-workflow/scripts/node_cache_pr_native_catalog.py materialize\n'
                'python3 .ci-workflow/scripts/node_compile_cache.py configure '
                '--catalog "$RUNNER_TEMP/cs-reviewed-native-catalog.json"\n'
                'python3 .ci-workflow/scripts/node_cache_pr_source.py record\n')
        if step.get('name') == 'Prepare fresh per-job compilation paths':
            step['run']=step['run'].replace('"$RUNNER_TEMP/cs-reviewed-entry.json"; do',
                '"$RUNNER_TEMP/cs-reviewed-entry.json" "$RUNNER_TEMP/cs-reviewed-native-catalog.json"; do')
        if step.get('name') == 'Upload readable coverage UT report':
            step['with']['path']+='${{ runner.temp }}/cs-node-cache/pr-source.json\n'
            step['with']['path']+='${{ runner.temp }}/cs-reviewed-native-catalog.json\n'
        if step.get('id') == 'coverage_ut':
            step['env']['CS_COMPILE_CACHE_HIT'] = 'false'
            step['env']['COVERAGE_GROUP_COUNT'] = '2'
            step['run'] = step['run'].replace('cd "$GITHUB_WORKSPACE"\n',
                'cd "$GITHUB_WORKSPACE"\ntest "${CS_CACHE_ENV:-}" = "$CS_S3_ENV"\n', 1)
        if step.get('id') == 'node_cache_acceptance':
            # Ordinary arbitrary PR SHAs may cold-miss; corruption must still fail.
            step['run'] = ('set -euo pipefail\npython3 .ci-workflow/scripts/node_compile_cache.py accept '
                           '--state "$RUNNER_TEMP/cs-node-cache/state.json"\n')
    merge = workflow['jobs']['merge-coverage-groups']
    for step in merge['steps']:
        if 'EXPECTED_HEAD_SHA' in step.get('env', {}):
            step['env']['EXPECTED_HEAD_SHA'] = '${{ github.event.pull_request.head.sha }}'
    def replace(value):
        if isinstance(value, dict): return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list): return [replace(v) for v in value]
        if isinstance(value, str):
            for before, after in {
                'inputs.node_compile_cache': 'true', 'inputs.coverage_compile_cache': 'false',
                'inputs.node_cache_candidate': 'false', 'inputs.group_count': '2',
                'inputs.node_cache_environment': "'auto'", 'inputs.source_sha': 'github.event.pull_request.head.sha',
                'steps.restore_reviewed_cache.outputs.hit': 'false',
            }.items(): value = value.replace(before, after)
        return value
    return replace(workflow)


class Dumper(yaml.SafeDumper):
    pass


def string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)


Dumper.add_representer(str, string)


def render():
    return '# Generated by scripts/build_pr_node_cache_workflow.py; existing entrypoints are unchanged.\n' + yaml.dump(
        build(), Dumper=Dumper, sort_keys=False, width=120)


if __name__ == '__main__':
    print(render(), end='')
