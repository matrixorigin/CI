"""Dedicated trusted native-only preparation, separate from ordinary PR UT."""
import copy
import yaml
import build_pr_node_cache_workflow as consumer

TARGET='.github/workflows/node-cache-native-refresh.yaml'


def build():
    old=yaml.safe_load((consumer.ROOT/'.github/workflows/node-cache-cpu-profile-producer.yaml').read_text())
    bootstrap=copy.deepcopy(old['jobs']['compile-only']['steps'][0])
    bootstrap['name']='One-time native preparation system dependencies (separate cost)'
    bootstrap['run']=bootstrap['run'].replace("${{ inputs.node }}","${{ matrix.node }}")
    request=__import__('json').loads((consumer.ROOT/'config/node-native-refresh-request.json').read_text())
    checkout='actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10'
    steps=[bootstrap,
        {'name':'Checkout exact canonical trusted native source','uses':checkout,
            'with':{'repository':'matrixorigin/matrixone','ref':request['source_sha'],'persist-credentials':False}},
        {'name':'Checkout immutable native-only helpers','uses':checkout,
            'with':{'repository':'${{ job.workflow_repository }}','ref':'${{ job.workflow_sha }}',
                'token':'${{ secrets.TOKEN_ACTION || github.token }}','path':'.ci-workflow',
                'persist-credentials':False,'sparse-checkout':'scripts\nconfig\n','sparse-checkout-cone-mode':True}},
        {'name':'Restore exact persistent Go and CMake without external downloads','timeout-minutes':5,
            'run':'set -euo pipefail\n'
                'python3 .ci-workflow/scripts/node_cache_toolchain_store.py --catalog .ci-workflow/config/node-compile-cache-catalog.json '
                '--lock .ci-workflow/config/node-toolchain-lock.json --destination "$RUNNER_TEMP/node-toolchain-install" '
                '--report "$RUNNER_TEMP/persistent-toolchain.json"\n'
                'for path in cs-buildcache-gomod cs-buildcache-go cs-node-cache native-only-candidate; do test ! -e "$RUNNER_TEMP/$path"; done\n'
                'echo "GOMODCACHE=$RUNNER_TEMP/cs-buildcache-gomod" >> "$GITHUB_ENV"\n'
                'echo "GOCACHE=$RUNNER_TEMP/cs-buildcache-go" >> "$GITHUB_ENV"\n'},
        {'name':'Require exact observed native key, CPU, tools and readonly node binding','timeout-minutes':5,
            'env':{'CS_CACHE_READ_TOKEN':'${{ secrets.CS_COVERAGE_CACHE_READ_TOKEN || github.token }}'},
            'run':'set -euo pipefail\n'
                'python3 .ci-workflow/scripts/node_compile_cache.py configure --catalog .ci-workflow/config/node-compile-cache-pr-catalog.json\n'
                'python3 .ci-workflow/scripts/node_cache_native_refresh.py preflight --state "$RUNNER_TEMP/cs-node-cache/state.json" '
                '--request .ci-workflow/config/node-native-refresh-request.json --catalog .ci-workflow/config/node-compile-cache-pr-catalog.json\n'},
        {'name':'Build only missing native outputs privately, no UT or Go precompile','timeout-minutes':30,
            'run':'set -euo pipefail\npython3 .ci-workflow/scripts/node_compile_cache.py prepare-native --state "$RUNNER_TEMP/cs-node-cache/state.json"\n'},
        {'name':'Freeze native-only unpublished candidate, excluding all Go actions','timeout-minutes':15,
            'run':'set -euo pipefail\n'
                'python3 .ci-workflow/scripts/node_cache_native_refresh.py freeze --state "$RUNNER_TEMP/cs-node-cache/state.json" '
                '--request .ci-workflow/config/node-native-refresh-request.json --destination "$RUNNER_TEMP/native-only-candidate"\n'},
        {'name':'Upload UNPUBLISHED candidate for independent protected review',
            'uses':'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
            'with':{'name':'native-only-candidate-${{ matrix.node }}-${{ github.run_id }}',
                'path':'${{ runner.temp }}/native-only-candidate/manifest.json\n${{ runner.temp }}/native-only-candidate/delta.tar.gz',
                'if-no-files-found':'error','compression-level':0,'retention-days':7}},
        {'name':'Preserve preparation and exact-key evidence','if':'${{ always() }}',
            'uses':'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
            'with':{'name':'native-refresh-evidence-${{ matrix.node }}-${{ github.run_id }}',
                'path':'${{ runner.temp }}/system-dependency-bootstrap.json\n${{ runner.temp }}/persistent-toolchain.json\n'
                    '${{ runner.temp }}/cs-node-cache/state.json\n${{ runner.temp }}/cs-node-cache/native-result.json\n'
                    '${{ runner.temp }}/cs-node-cache/native-refresh-preflight.json\n${{ runner.temp }}/cs-node-cache/failure-reports',
                'if-no-files-found':'error','retention-days':7}}]
    return {'name':'Dedicated exact-input native-only refresh (not PR UT)',
        'on':{'workflow_call':{'secrets':{'CS_COVERAGE_CACHE_READ_TOKEN':{'required':False},'TOKEN_ACTION':{'required':False}}}},
        'permissions':{'contents':'read','actions':'read'},'jobs':{
            'authorize':{'if':"github.repository == 'matrixorigin/matrixone' && github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/weilu-runner-test' && github.actor == 'Ariznawlll' && github.run_attempt == 1",
                'runs-on':'ubuntu-latest','outputs':{'allowed':'${{ steps.scope.outputs.allowed }}'},
                'steps':[{'id':'scope','run':"echo 'allowed=true' >> \"$GITHUB_OUTPUT\""}]},
            'native-only':{'needs':'authorize','if':"needs.authorize.outputs.allowed == 'true'",
                'strategy':{'fail-fast':False,'max-parallel':2,'matrix':{'node':['mo-standard-0','mo-standard-1']}},
                'runs-on':"${{ matrix.node == 'mo-standard-0' && 'cs-mty-nc-n0-pr-v1' || 'cs-mty-nc-n1-pr-v1' }}",
                'timeout-minutes':70,'env':{'GOMAXPROCS':'7','GOTOOLCHAIN':'local','GOPROXY':'https://goproxy.cn|https://proxy.golang.org|direct',
                    'CGO_ENABLED':'1','GOFLAGS':'','GOWORK':'off','COVERAGE_GROUP':'0','COVERAGE_UT_CGO_TIMEOUT_SECONDS':'1800',
                    'CS_CACHE_SOURCE_REPOSITORY':'matrixorigin/matrixone'},'steps':steps}}}


def render():
    return '# Generated by scripts/build_native_refresh_workflow.py; no ordinary PR compilation/export change.\n'+yaml.dump(
        build(),Dumper=consumer.Dumper,sort_keys=False,width=120)


if __name__=='__main__':print(render(),end='')
