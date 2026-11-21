"""Unprivileged hints, protected reconciliation and durable single-flight claims.

No cluster/network access. A hint is never a build target or permission to write.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path
from uuid import UUID

RECIPE = 'cs-coverage-two-groups-v1:48edf24e44a888c1c4d57318c4eca7cbb310c6e7'
FIELDS = {'site','node','node_uid','pvc_uid','pv_uid','runtime','native','modules','recipe'}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def checked(profile):
    require(type(profile) is dict and set(profile) == FIELDS, 'unknown profile fields')
    require(profile['site'] in ('mty','jed','ruh','mnl2') and profile['node'] in
            ('mo-standard-0','mo-standard-1','mo-standard-2') and
            (profile['site'],profile['node']) != ('mty','mo-standard-2'), 'unsupported reader')
    for key in ('node_uid','pvc_uid','pv_uid'):
        require(str(UUID(profile[key])) == profile[key], 'invalid volume/node identity')
    for key in ('runtime','native','modules'):
        require(re.fullmatch('[0-9a-f]{64}',profile[key]), 'invalid fingerprint')
    require(profile['recipe'] == RECIPE, 'different compile contract')
    return profile


def load(path, limit):
    with path.open('rb') as f:
        raw = f.read(limit+1)
    require(len(raw) <= limit, 'oversized input')
    return json.loads(raw)


def modules(repo, commit):
    require(re.fullmatch('[0-9a-f]{40}',commit), 'immutable commit required')
    lines = subprocess.check_output(['git','ls-tree',commit,'--','go.mod','go.sum'], cwd=repo,
                                    timeout=10, text=True).splitlines()
    require(len(lines)==2 and all(re.fullmatch(r'100644 blob [a-f0-9]{40}\tgo\.(mod|sum)',l)
                                  for l in lines), 'invalid module input tree')
    return digest(lines)


def observe(temp, repo):
    state = load(temp/'cs-node-cache/state.json',8<<20)
    source = load(temp/'cs-node-cache/pr-source.json',4096)
    auth = load(temp/'cs-node-cache/persistent-authorization.json',64<<10)
    catalog = load(temp/'cs-reviewed-persistent-catalog.json',16<<20)
    tools = load(temp/'persistent-toolchain.json',64<<10)
    binding, cfg = tools['binding'], state['config']
    require(tools['persistent_tools'] is True and binding in catalog['nodes'] and
            cfg['shared']=='/ci-cache/incremental-v2' and binding['environment']==cfg['runtime']['environment']
            and cfg['runtime']['repository']=='matrixorigin/matrixone', 'unverified reader')
    receipt = temp/'cs-node-cache/release-selection.json'
    released = load(receipt,16384) if receipt.is_file() else None
    extra = [released['index_sha256']] if released else []
    require(auth.get('authorization_id') and auth.get('revocations_sha256') and
            sorted(auth['authorized_indexes']+extra)==sorted(cfg['indexes']), 'authorization unavailable')
    bound = {k:binding[k] for k in ('node_uid','pv_uid','pvc_uid')}
    entries = [e for e in catalog['entries'] if bound in e['bindings']]
    compatible = [e for e in entries if e['runtime']==cfg['runtime']]
    require(entries and {e['index_sha256'] for e in compatible} == set(auth['authorized_indexes']),
            'unselected or revoked generation, not refreshable')
    native = state['native_key']
    require(state.get('native_authorities',{}).get(native,True) is not None,'native authority withheld')
    head = source['checked_out_head_sha']
    require(head==source['actual_pr_head_sha'] and source['shared_cache_writes']==0, 'source mismatch')
    profile = checked(dict(**bound,site=binding['environment'],node=binding['node'],
                           runtime=digest(cfg['runtime']),native=native,modules=modules(repo,head),
                           recipe=RECIPE))
    if released:
        require(released['profile_key']==digest(profile),'release receipt does not match this reader')
    return {'schema':1,'profile':profile,'head':head,'runtime_missing':not (compatible or released),
            'native_missing':not (any(native in e['native_manifests'] for e in compatible)
                                  or (released and native in released['native_manifests']))}


def plan(hint, accepted, trusted):
    """trusted comes ONLY from a protected independent builder/inventory.

    It must rebuild the same fingerprints against a trusted upstream commit.
    An unmerged fork-only dependency waits; it never becomes trusted source.
    """
    require(hint['schema']==1 and type(hint['runtime_missing']) is bool and
            type(hint['native_missing']) is bool,'invalid hint')
    profile = checked(hint['profile'])
    if profile != checked(trusted['profile']):
        return 'waiting-for-compatible-trusted-source'
    require(re.fullmatch('[0-9a-f]{40}',trusted['upstream_commit']), 'unpinned source')
    require(all(type(trusted[k]) is bool for k in ('admitted','runtime_missing','native_missing',
                'module_profile_missing')), 'invalid protected observation')
    if not trusted['admitted']:
        return 'hold'
    if not any(trusted[k] for k in ('runtime_missing','native_missing','module_profile_missing')):
        return 'already-covered'
    shared, compiled, native_hit = accepted['shared_hits'],accepted['go_compiler_invocations'],accepted['native_hit']
    require(type(shared) is int and shared>=0 and type(compiled) is int and compiled>=0
            and type(native_hit) is bool,'incomplete UT evidence')
    if ((hint['runtime_missing'] and trusted['runtime_missing'] and shared==0 and compiled>0)
            or (hint['native_missing'] and trusted['native_missing'] and not native_hit)
            or (trusted['module_profile_missing'] and compiled>0)):
        return 'prepare'
    return 'no-confirmed-miss'


def claim(db, hint, accepted, trusted):
    decision=plan(hint,accepted,trusted)
    if decision!='prepare':
        return {'decision':decision}
    key=digest(checked(trusted['profile']))
    # Private, persistent controller storage. One winner across concurrent PRs,
    # group retries and controller restarts. Never automatically reclaim an
    # ambiguous dispatch; reconcile the receipt first.
    db.execute("""CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, upstream TEXT NOT NULL,
               phase TEXT NOT NULL, proof TEXT NOT NULL)""")
    with db:
        inserted=db.execute("INSERT INTO intents VALUES (?,?,'claimed','{}') ON CONFLICT(id) DO NOTHING",
                            (key,trusted['upstream_commit'])).rowcount
    return {'decision':'claimed' if inserted else 'duplicate','key':key}


def reconcile(db,hint,accepted,backend):
    """Run only on the protected controller. Never pass fork identity or executable paths from a report."""
    profile=checked(hint['profile'])
    live=backend('inspect',{'profile':profile})
    result=claim(db,hint,accepted,live)
    if result['decision'] not in ('claimed','duplicate'):
        return result
    key=result['key']
    request={'key':key,'profile':profile,'upstream_commit':live['upstream_commit']}
    # Durable intent BEFORE each non-repeatable call. If a call fails or the
    # process dies, require reconciliation by key, not an automatic second write.
    ops=['verify_candidate','append_missing','readback','activate']
    if result['decision']=='duplicate':
        # Resume only a confirmed private build. Never replay an ambiguous operation.
        row=db.execute('SELECT upstream,phase,proof FROM intents WHERE id=?',(key,)).fetchone()
        if row[1]!='awaiting-registration' or row[0]!=live['upstream_commit']:
            return result
        saved=json.loads(row[2])
        require(saved['profile']==profile and saved['key']==key,'ledger identity mismatch')
        if live.get('trusted_candidate_index') != saved['index_sha256']:
            return {'decision':'awaiting-registration','key':key}
        with db:
            won=db.execute("UPDATE intents SET phase='registration:verified' WHERE id=? AND phase='awaiting-registration'",
                           (key,)).rowcount
        if not won: return result
        request=saved
    elif live.get('trusted_candidate_index') is None:
        ops.insert(0,'prepare')
    else:
        require(type(live['trusted_candidate_index']) is str and
                re.fullmatch('[0-9a-f]{64}',live['trusted_candidate_index']), 'invalid trusted candidate')
        request['index_sha256']=live['trusted_candidate_index']
    for op in ops:
        with db:
            db.execute('UPDATE intents SET phase=? WHERE id=?',(op+':intent',key))
        receipt=backend(op,request)
        require(receipt.get('key')==key and receipt.get('upstream_commit')==live['upstream_commit']
                and receipt.get('operation')==op and receipt.get('success') is True,
                'protected operation did not confirm this request')
        generation=receipt.get('index_sha256')
        require(type(generation) is str and re.fullmatch('[a-f0-9]{64}',generation),
                'missing immutable index')
        require(request.get('index_sha256',generation)==generation,'cache generation changed')
        request['index_sha256']=generation
        wait = op=='prepare' and live.get('registration_required') is True
        with db:
            db.execute('UPDATE intents SET phase=?, proof=? WHERE id=?',
                       ('awaiting-registration' if wait else op+':verified',json.dumps(request,sort_keys=True),key))
        if wait:
            return {'decision':'awaiting-registration','key':key}
    return {'decision':'activated','key':key,'index_sha256':request['index_sha256']}


def protected_backend(operation, request):
    # Site-managed, never resolved from a command in a hint, a PR checkout or PATH.
    # Install each command in the dedicated controller image only after its
    # Kubernetes/GitHub identity and permissions have been approved.
    command=Path('/opt/matrixorigin/cache-refresh/bin')/operation
    require(command.is_file() and os.access(command,os.X_OK),'protected backend not installed')
    result=subprocess.run([str(command)],input=json.dumps(request),text=True,
                          stdout=subprocess.PIPE,check=True,timeout=7200)
    require(len(result.stdout)<=65536,'oversized protected receipt')
    return json.loads(result.stdout)


def report_inputs(path):
    """Read allowlisted JSON only. Treat even a genuine Actions report as untrusted."""
    require(path.stat().st_size <= 32<<20,'unexpected report size')
    with zipfile.ZipFile(path) as z:
        members=z.infolist()
        require(len(members)<=128,'too many report files')
        def read(name,limit,optional=False):
            found=[m for m in members if m.filename==name]
            if optional and not found:
                return None
            require(len(found)==1 and 0<found[0].file_size<=limit,'ambiguous or oversized report input')
            return json.loads(z.read(found[0]))
        hint=read('cache-refresh-hint.json',16384,True)
        if hint is None:
            return None
        acceptance=read('cs-node-cache/acceptance.json',16384)
        native=read('cs-node-cache/native-result.json',16384)
        compile_counts=read('go-rebuild-counts.json',2<<20)
        return hint,dict(shared_hits=acceptance['shared_hits'],native_hit=native['native_hit'],
                         go_compiler_invocations=compile_counts['go_compiler_invocations'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    commands=p.add_subparsers(dest='mode',required=True)
    reader=commands.add_parser('observe')
    reader.add_argument('--temp',required=True,type=Path)
    reader.add_argument('--repo',required=True,type=Path)
    controller=commands.add_parser('plan')
    for k in ('hint','accepted','trusted'):
        controller.add_argument('--'+k,required=True,type=Path)
    live=commands.add_parser('reconcile')
    for k in ('report','ledger'):
        live.add_argument('--'+k,required=True,type=Path)
    a=p.parse_args()
    if a.mode=='observe':
        result=observe(a.temp,a.repo)
    elif a.mode=='plan':
        result={'decision':plan(*[load(path,16<<10) for path in (a.hint,a.accepted,a.trusted)])}
    else:
        inputs=report_inputs(a.report)
        if inputs is None:
            result={'decision':'no-hint'}
        else:
            with closing(sqlite3.connect(a.ledger,timeout=30)) as db:
                result=reconcile(db,*inputs,protected_backend)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':
    main()
