#!/usr/bin/env python3
"""Additional fail-closed evidence for isolated warm/source-change acceptance.

Never publishes, builds, edits source, or emits test output/credentials. The
production consumer's ordinary cold-miss behavior is deliberately unchanged.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def canary_evidence(events, package, test, assigned):
    seen=[]
    for line in events:
        event=json.loads(line)
        assert isinstance(event,dict) and isinstance(event.get('Action'),str)
        if event.get('Package')==package and event.get('Test')==test and event['Action'] in ('run','pass','skip','fail'):
            seen.append(event['Action'])
    assert seen==(['run','pass'] if assigned else []),'controlled test did not execute exactly once in its assigned group'
    return {'assigned':assigned,'runs':seen.count('run'),'passes':seen.count('pass')}


def validate(policy,state,native,acceptance,source_sha,source,packages,events):
    assert source_sha in policy['source_shas']
    assert native['native_hit'] is True,'warm acceptance requires real native restore'
    assert acceptance['shared_hits']>0 and acceptance['copied_bytes']>0
    assert state['entries'] and state['config']['indexes']
    assert set(state['config']['indexes'])=={e['index_sha256'] for e in state['entries']}
    assert all(e['compile_only'] is True for e in state['entries'])
    authority=state['native_authorities'].get(state['native_key'])
    assert authority and any(e['native_manifests'].get(state['native_key'])==authority for e in state['entries'])
    assert len(packages)==len(set(packages)) and packages
    change=policy['controlled_sources'].get(source_sha)
    proof=None
    if change is not None:
        assert set(change)=={'baseline_sha','files','canary_package','canary_test'}
        assert change['baseline_sha'] in policy['source_shas'] and change['baseline_sha']!=source_sha
        for name,digest in change['files'].items():
            path=source/name
            assert not path.is_symlink() and source.resolve() in path.resolve().parents
            assert hashlib.sha256(path.read_bytes()).hexdigest()==digest,'controlled source content differs'
        assert any(e['source']==change['baseline_sha'] for e in state['entries']), 'no baseline cache selected'
        proof=canary_evidence(events,change['canary_package'],change['canary_test'],change['canary_package'] in packages)
    return {'schema':1,'source_sha':source_sha,'native_hit':True,'native_manifest_sha256':authority,
        'native_output_sha256':native['native_output_sha256'],
        'selected_indexes':state['config']['indexes'],'producer_sources':sorted({e['source'] for e in state['entries']}),
        'shared_hits':acceptance['shared_hits'],'copied_bytes':acceptance['copied_bytes'],
        'package_count':len(packages),'packages_sha256':hashlib.sha256(('\n'.join(packages)+'\n').encode()).hexdigest(),
        'controlled_source':proof,'accepted':True,'tests_cached':False,'shared_writes':False}


def main():
    parser=argparse.ArgumentParser()
    for name in ('policy','state','source','packages','report','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    read=lambda p:json.loads(p.read_text())
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.source,text=True,timeout=15).strip()
    assert not args.output.exists()
    with args.report.open() as events:
        result=validate(read(args.policy),read(args.state),read(args.state.parent/'native-result.json'),
            read(args.state.parent/'acceptance.json'),sha,args.source,args.packages.read_text().splitlines(),events)
    args.output.write_text(json.dumps(result,sort_keys=True)+'\n')
    print('Isolated incremental validation: native restore, shared object reuse and controlled source evidence passed.')


if __name__=='__main__':
    try:main()
    except Exception:
        print('Isolated incremental validation rejected; no fallback or publication.')
        raise SystemExit(1)
