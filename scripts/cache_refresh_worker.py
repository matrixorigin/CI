"""Read-only admission for asynchronous refresh after a production CS worker."""
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from . import cache_refresh_demand as demand
from . import coverage_standby as standby

check = demand.require
HEX = r'[0-9a-f]{40}'
LIMIT = 32 << 20


def route(inputs, head, runner):
    if inputs is None:
        return 'no-hint'
    h, a = inputs
    p = demand.checked(h['profile'])
    check(h['head'] == head and h['schema'] == 1 and
          all(type(h[k]) is bool for k in ('native_missing', 'runtime_missing')), 'invalid hint')
    check(standby.assigned({'runner_id': 1, 'runner_name': runner}, 'cs') and
          runner.startswith(f"cs-{p['site']}-nc-n{p['node'][-1]}-pr-v1-"), 'wrong reader node')
    shared, compiled, native = a['shared_hits'], a['go_compiler_invocations'], a['native_hit']
    check(type(shared) is int and shared >= 0 and type(compiled) is int and compiled >= 0
          and type(native) is bool, 'invalid reader counters')
    if (h['native_missing'] and not native) or (compiled and h['runtime_missing'] and not shared):
        return 'inspect'
    # Some private compilation is normal. Only protected inventory can tell
    # whether an upstream module profile (rather than fork-only code) is absent.
    return 'inspect' if compiled else 'no-confirmed-miss'


def archive(item, run, name):
    size = item['size_in_bytes']
    check(item['name'] == name and item['workflow_run']['id'] == run and not item['expired']
          and 0 < size <= LIMIT and re.fullmatch(r'sha256:[0-9a-f]{64}', item['digest']),
          'invalid report metadata')
    try:
        standby.api(f"/actions/artifacts/{int(item['id'])}/zip")
    except urllib.error.HTTPError as err:
        if err.code != 302:
            raise
        location = err.headers['Location']
    else:
        raise ValueError('expected signed artifact redirect')
    u = urlsplit(location)
    check(u.scheme == 'https' and u.username is None and u.password is None and
          (u.hostname or '').endswith(('.blob.core.windows.net', '.actions.githubusercontent.com',
                                       '.githubusercontent.com')), 'unexpected artifact host')
    # No Authorization header on the signed blob request; no second redirect.
    with urllib.request.build_opener(standby.NoRedirect()).open(location, timeout=60) as response:
        raw = response.read(size + 1)
    check(len(raw) == size and 'sha256:' + hashlib.sha256(raw).hexdigest() == item['digest'],
          'wrong report digest')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'report.zip'
        path.write_bytes(raw)
        return demand.report_inputs(path)


def evidence(run, pin):
    check(run > 0 and re.fullmatch(HEX, pin), 'invalid run or implementation pin')
    m = standby.run(run)
    check(m['id'] == run and m['repository']['full_name'] == m['head_repository']['full_name']
          == standby.REPO and m['event'] == 'workflow_dispatch' and m['head_branch'] == 'main'
          and m['path'] == '.github/workflows/' + standby.WORKER and m['run_attempt'] == 1
          and m['status'] == 'completed' and m['conclusion'] == 'success', 'not an eligible worker')
    match = re.fullmatch(r'Coverage standby / ([1-9][0-9]*)/([1-9][0-9]*)/([01])/(' + HEX +
                         r')/(cs|idc)/([0-9a-f]{64})', m['display_title'])
    check(match, 'invalid worker title')
    parent, attempt, group, head, pool, key = match.groups()
    if pool != 'cs':
        return 'not-cs', None
    expected = 'matrixorigin/CI/.github/workflows/coverage-worker-cs.yaml@' + pin
    check(any(w['path'] == expected and w['sha'] == pin for w in m['referenced_workflows']),
          'CS worker implementation changed')
    grant = standby.artifact(int(parent), f'coverage-admit-{run}', 'grant.json')
    check(grant and grant['pool'] == pool and grant['child'] == run and
          standby.digest(grant['nonce']) == key, 'missing worker grant')
    t = grant['ticket']
    check((t['parent'], t['attempt'], t['group'], t['head'], t['key'], t['revision']) ==
          (int(parent), int(attempt), int(group), head, key, m['head_sha']), 'grant mismatch')
    standby.parent_identity(t, standby.one(f'/actions/runs/{int(parent)}/attempts/{int(attempt)}'))
    job, _ = standby.observe(run, t, pool)
    check(job and job['conclusion'] == 'success' and standby.assigned(job, pool), 'wrong worker')
    name = f'ut-coverage-report-group-{group}-attempt-1'
    items = standby.one(f'/actions/runs/{run}/artifacts?name={name}&per_page=2')['artifacts']
    check(len(items) == 1, 'missing or ambiguous reader report')
    inputs = archive(items[0], run, name)
    return route(inputs, head, job['runner_name']), inputs


def main():
    mode, = sys.argv[1:]
    check(mode in ('screen', 'publish'), 'invalid mode')
    decision, inputs = evidence(int(os.environ['WORKER_RUN']), os.environ['CI_PIN'])
    os.environ.pop('ACTIONS_TOKEN', None)
    if mode == 'screen':
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            print('decision=' + decision, file=output)
    elif decision == 'inspect':
        # The operator mounts its private ledger, independently evaluates the
        # approved source/node and owns the only writer. No PR bytes are copied.
        with closing(sqlite3.connect('/var/lib/matrixorigin/cache-refresh/ledger.sqlite',
                                     timeout=30)) as db:
            decision = demand.reconcile(db, *inputs, demand.protected_backend)['decision']
    print('cache refresh: ' + decision)


if __name__ == '__main__':
    main()
