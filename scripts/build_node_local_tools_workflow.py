"""Separate opt-in workflow; the running relay/producer revisions are unchanged."""
from pathlib import Path
import sys
import yaml
import build_node_consumer_workflow as consumer


def build(helper):
    doc=consumer.build(helper)
    doc['name']='Isolated two-group consumer with persistent local tools'
    del doc['jobs']['prepare-toolchain']
    job=doc['jobs']['ut-coverage-linux-x86']
    job['needs'].remove('prepare-toolchain')
    steps=job['steps']
    steps[:]=[s for s in steps if s.get('name')!='Download verified toolchain artifact']
    checkout=next(s for s in steps if s.get('with',{}).get('path')=='.ci-toolchain')
    checkout['with']['sparse-checkout']='scripts\nconfig'
    checkout['with']['sparse-checkout-cone-mode']=True
    restore=next(s for s in steps if s.get('name')=='Restore exact reviewed tools (no external release download)')
    restore.clear()
    restore.update(name='Restore verified persistent tools from read-only node PVC (no network)',
        id='persistent_toolchain',**{'timeout-minutes':5},
        run='''set -euo pipefail
python3 .ci-toolchain/scripts/node_cache_toolchain_store.py --catalog .ci-toolchain/config/node-compile-cache-catalog.json --lock .ci-toolchain/config/node-toolchain-lock.json --destination "$RUNNER_TEMP/node-toolchain-install" --report "$RUNNER_TEMP/persistent-toolchain.json"
''')
    steps.append({'name':'Upload persistent tool recovery evidence','if':'always()',
        'uses':'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
        'with':{'name':'persistent-tools-${{ inputs.node_cache_environment }}-${{ matrix.group }}-${{ github.run_id }}',
                'path':'${{ runner.temp }}/persistent-toolchain.json','if-no-files-found':'ignore','retention-days':7}})
    steps.append({'name':'Upload allowlisted early cache failure evidence','if':'failure()',
        'uses':'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
        'with':{'name':'cache-failure-${{ inputs.node_cache_environment }}-${{ matrix.group }}-${{ github.run_id }}',
                'path':'${{ runner.temp }}/cs-node-cache/failure-reports/*.json','if-no-files-found':'ignore','retention-days':7}})
    return doc


if __name__=='__main__':
    (consumer.ROOT/'.github/workflows/node-cache-local-tools-acceptance.yaml').write_text(
        yaml.safe_dump(build(sys.argv[1]),sort_keys=False,width=130))
