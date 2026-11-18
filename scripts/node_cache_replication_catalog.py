"""Pure proposal builder after independently audited node-copy receipts.

Never approves producer data, runs jobs, writes caches or silently replaces an
old binding. Protected orchestration authenticates each receipt first. The
returned catalogue still requires review/commit before any runner can select it.
"""
import copy
import hashlib
import json


def propose(catalog,receipts):
    result=copy.deepcopy(catalog)
    assert result['schema']==2
    seen=set()
    for env,receipt in receipts.items():
        assert env in ('mty','jed','ruh','mnl2') and env==receipt['environment'] and env not in seen
        seen.add(env)
        assert receipt['complete'] is True and receipt['helper_pods_absent'] is True
        assert receipt['provenance_verified_before_write'] is True and receipt['compiler_invocations']==0
        assert receipt['deleted']==[r for r in receipt['created'] if r['kind']=='Pod']
        assert 'pending_create' not in receipt
        assert set(receipt['targets'])=={'mo-standard-1','mo-standard-2'}
        original=next(n for n in catalog['nodes'] if n['environment']==env and n['node']=='mo-standard-0')
        source_binding={k:original[k] for k in ('node_uid','pvc_uid','pv_uid')}
        entries=[e for e in result['entries'] if e['runtime']['environment']==env and source_binding in e.get('bindings',[])]
        expected=sorted(e['index_sha256'] for e in entries)
        assert expected and len(set(expected))==len(expected)
        authorities=[a for a in catalog.get('native_authorities',[]) if a['environment']==env and a['node']=='mo-standard-0']
        for number in (1,2):
            node=f'mo-standard-{number}';row=receipt['targets'][node];proof=row['verify']
            assert row['claim']==f'cs-{env}-incremental-n{number}-v1'
            assert row['received']==proof and proof['success'] is True and proof['independently_verified'] is True
            assert proof['compiler_invocations']==proof['external_downloads']==0 and proof['indexes']==expected
            identity=row['binding'];assert set(identity)=={'environment','node','node_uid','pvc_uid','pv_uid'}
            assert identity['environment']==env and identity['node']==node
            raw=json.dumps(identity,sort_keys=True,separators=(',',':')).encode()
            binding={**identity,'marker_sha256':hashlib.sha256(raw).hexdigest()}
            assert proof['binding']==binding
            assert not any(n['environment']==env and n['node']==node for n in result['nodes']), 'existing node binding must never be replaced'
            assert not any(n['pvc_uid']==identity['pvc_uid'] or n['pv_uid']==identity['pv_uid'] for n in result['nodes'])
            made=[x for x in receipt['created'] if x['kind']=='PersistentVolumeClaim' and x['name']==row['claim']]
            assert len(made)==1 and made[0]['uid']==identity['pvc_uid']
            result['nodes'].append(binding)
            target_binding={k:identity[k] for k in ('node_uid','pvc_uid','pv_uid')}
            for entry in entries:
                assert target_binding not in entry['bindings'];entry['bindings'].append(target_binding)
            for authority in authorities:
                assert authority['index_sha256'] in expected
                result.setdefault('native_authorities',[]).append({**authority,**identity})
    return result
