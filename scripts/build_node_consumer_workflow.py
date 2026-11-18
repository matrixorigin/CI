"""Mechanical isolated consumer variant; never edit the running producer."""
import copy
from pathlib import Path
import re
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]


def build(helper):
    assert re.fullmatch('[0-9a-f]{40}', helper)
    doc = yaml.safe_load((ROOT/'.github/workflows/node-cache-full-acceptance.yaml').read_text())
    doc['name'] = 'Isolated two-group node cache consumer (no publication)'
    gate = doc['jobs']['check_coverage_eligibility']
    shell = gate['steps'][-1]['run']
    shell = shell.replace('case "$GROUP_COUNT" in 2|3) ;; *) exit 1 ;; esac', 'test "$GROUP_COUNT" = 2')
    gate['steps'][-1]['run'] = shell.replace('test "$CANDIDATE" = true', 'test "$CANDIDATE" = false')
    doc['on']['workflow_call']['inputs']['node_cache_candidate']['default'] = False
    doc['on']['workflow_call']['inputs']['node_cache_candidate']['description'] = 'Must be false; consumer never exports or publishes cache'
    job = doc['jobs']['ut-coverage-linux-x86']
    job['needs'].append('prepare-toolchain')
    job['strategy']['max-parallel'] = 2
    job['strategy']['matrix']['group'] = [0, 1]
    steps = job['steps']
    steps[:] = [s for s in steps if s.get('id') != 'upload_node_cache_candidate']
    ut = next(s for s in steps if s.get('id') == 'coverage_ut')
    start = ut['run'].index("if [ '${{ inputs.node_cache_candidate }}' = true ]; then")
    end = ut['run'].index('# Fail early on the real', start)
    ut['run'] = ut['run'][:start] + ut['run'][end:]
    setup_index = next(i for i,s in enumerate(steps) if s.get('name') == 'Set up Go')
    checkout = {'uses':'actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10',
                'with':{'repository':'matrixorigin/CI','ref':helper,'persist-credentials':False,
                        'path':'.ci-toolchain','sparse-checkout':'scripts/node_cache_toolchain.py\nconfig/node-compile-cache-catalog.json',
                        'sparse-checkout-cone-mode':False}}
    steps[setup_index:setup_index+1] = [copy.deepcopy(checkout),
        {'name':'Download verified toolchain artifact','timeout-minutes':10,
         'uses':'actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093',
         'with':{'name':'node-toolchain-${{ github.run_id }}-${{ github.run_attempt }}','path':'${{ runner.temp }}/node-toolchain-download'}},
        {'name':'Restore exact reviewed tools (no external release download)','timeout-minutes':5,
         'env':{'TOOLCHAIN_SHA':'${{ needs.prepare-toolchain.outputs.sha256 }}'},
         'run':'set -euo pipefail\npython3 .ci-toolchain/scripts/node_cache_toolchain.py restore --catalog .ci-toolchain/config/node-compile-cache-catalog.json --root "$RUNNER_TEMP/node-toolchain-install" --archive "$RUNNER_TEMP/node-toolchain-download/toolchain.tar.gz" --sha "$TOOLCHAIN_SHA"'}]
    doc['jobs']['prepare-toolchain'] = {
        'needs':['check_coverage_eligibility'], 'if':"needs.check_coverage_eligibility.outputs.allowed == 'true'",
        'runs-on':'ubuntu-latest','timeout-minutes':15,
        'outputs':{'sha256':'${{ steps.pack.outputs.sha256 }}'},
        'steps':[copy.deepcopy(checkout),
            {'uses':'actions/setup-go@40f1582b2485089dde7abd97c1529aa768e1baff',
             'with':{'go-version':'1.27.1','cache':False}},
            {'uses':'matrixorigin/CI/actions/setup-cmake@37298579134ab5ce90a4a4df5a40d6e6ed393ebf',
             'with':{'cmake':'3.17.2','ninja':'1.9.0'}},
            {'id':'pack','name':'Package exact reviewed compiler tools only',
             'run':'''set -euo pipefail
mkdir "$RUNNER_TEMP/node-toolchain-stage"
cp -a "$(go env GOROOT)" "$RUNNER_TEMP/node-toolchain-stage/go"
cmake_root=$(dirname "$(dirname "$(readlink -f "$(command -v cmake)")")")
cp -a "$cmake_root" "$RUNNER_TEMP/node-toolchain-stage/cmake"
python3 .ci-toolchain/scripts/node_cache_toolchain.py pack --catalog .ci-toolchain/config/node-compile-cache-catalog.json --root "$RUNNER_TEMP/node-toolchain-stage" --archive "$RUNNER_TEMP/toolchain.tar.gz"
'''},
            {'uses':'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
             'with':{'name':'node-toolchain-${{ github.run_id }}-${{ github.run_attempt }}',
                     'path':'${{ runner.temp }}/toolchain.tar.gz','if-no-files-found':'error','retention-days':7}}]}
    for j in doc['jobs'].values():
        for s in j.get('steps',[]):
            if s.get('with',{}).get('repository') == 'matrixorigin/CI':
                s['with']['ref'] = helper
    return doc


if __name__ == '__main__':
    target = ROOT/'.github/workflows/node-cache-consumer-acceptance.yaml'
    target.write_text(yaml.safe_dump(build(sys.argv[1]), sort_keys=False, width=130))
