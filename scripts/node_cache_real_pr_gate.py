"""Authorize one actual open PR snapshot for the isolated nightly validator.

Read-only GitHub metadata; no PR labels/comments/merge or workflow dispatch.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

import incremental_cache as c

OBSERVATION={}


def authorize(context,pr,membership):
    c.require(context['repository']=='matrixorigin/matrixone' and context['event']=='workflow_dispatch'
        and context['ref']=='refs/heads/weilu-runner-test' and context['actor']=='Ariznawlll'
        and context['attempt']=='1','isolated PR validator caller authority differs')
    c.require(type(context['pr_number']) is int and context['pr_number']>0 and pr['number']==context['pr_number']
        and pr['state']=='open' and pr['draft'] is False and pr['base']['ref']=='main'
        and pr['base']['repo']['full_name']=='matrixorigin/matrixone','not a current non-draft main PR')
    sha=context['expected_sha'];repository=pr['head']['repo']['full_name'];author=pr['user']['login']
    c.require(re.fullmatch('[0-9a-f]{40}',sha) and pr['head']['sha']==sha
        and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository)
        and pr['head']['repo']['archived'] is False and re.fullmatch('[A-Za-z0-9_-]+',author),
        'PR source snapshot/repository identity changed')
    c.require(membership==204 and 'kind/test-ci' in {r['name'] for r in pr['labels']},
        'PR author membership or existing safe CI label absent')
    return {'schema':1,'allowed':'true','head_sha':sha,'head_repository':repository,
        'pr_number':str(pr['number']),'author':author,'project_cache_namespace':'matrixorigin/matrixone',
        'verified_membership_http_status':204,
        'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}


def read(path,token=None):
    c.require(re.fullmatch(r'(repos/matrixorigin/matrixone/pulls/[0-9]+|orgs/matrixorigin/members/[A-Za-z0-9_-]+)',path),
        'unapproved PR metadata path')
    token=os.environ.get('CS_CACHE_READ_TOKEN','') if token is None else token
    c.require(bool(token),'PR metadata credential unavailable')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    request=urllib.request.Request('https://api.github.com/'+path,
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request,timeout=30) as response:
            raw=response.read(2*1024**2+1);c.require(len(raw)<=2*1024**2,'PR metadata response too large')
            return response.status,None if response.status==204 else json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code in (401,403,404):return error.code,None
        raise ValueError('PR metadata read unavailable') from None


def membership(author,cache_token,checkout_token,reader=read):
    """Require the SAME 204 proof using existing workflow credentials only.

    The Actions-artifact read token need not have private-org read permission.
    The already available fork-checkout token is an optional metadata-only
    fallback. No secret is written to a report and no non-204 is accepted.
    """
    c.require(re.fullmatch('[A-Za-z0-9_-]+',author),'PR author identity invalid')
    checks=[];status=None
    for role,token in (('actions_read',cache_token),('existing_source_checkout',checkout_token)):
        if not token or (role=='existing_source_checkout' and token==cache_token):continue
        status,_=reader('orgs/matrixorigin/members/'+author,token=token)
        checks.append({'credential_role':role,'http_status':status})
        if status==204:break
        c.require(status in (401,403,404),'unexpected org membership response; fail closed')
    return status,checks


def main():
    OBSERVATION['stage']='caller_context'
    number=int(os.environ['REQUESTED_PR_NUMBER'])
    context={'repository':os.environ['GITHUB_REPOSITORY'],'event':os.environ['GITHUB_EVENT_NAME'],
        'ref':os.environ['GITHUB_REF'],'actor':os.environ['GITHUB_ACTOR'],'attempt':os.environ['GITHUB_RUN_ATTEMPT'],
        'pr_number':number,'expected_sha':os.environ['EXPECTED_PR_HEAD_SHA']}
    c.require(0<number<10000000,'PR number outside review scope')
    OBSERVATION['stage']='pr_metadata'
    status,pr=read(f'repos/matrixorigin/matrixone/pulls/{number}')
    OBSERVATION['pr_metadata_http_status']=status;c.require(status==200,'PR metadata not found')
    author=pr['user']['login'];c.require(re.fullmatch('[A-Za-z0-9_-]+',author),'PR author identity invalid')
    OBSERVATION['stage']='org_membership'
    member_status,checks=membership(author,os.environ.get('CS_CACHE_READ_TOKEN',''),os.environ.get('CS_PR_METADATA_TOKEN',''))
    OBSERVATION['membership_checks']=checks
    OBSERVATION['stage']='exact_snapshot_authorization'
    value=authorize(context,pr,member_status)
    value['membership_checks']=checks
    c.atomic_json(Path(os.environ['RUNNER_TEMP'])/'real-pr-authorization.json',value)
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        for key in ('allowed','head_sha','head_repository','pr_number'):output.write(key+'='+value[key]+'\n')
    print(json.dumps({'authorized':True,'pr_number':number,'head_sha':value['head_sha'],'head_repository':value['head_repository'],
        'membership_checks':checks}))


if __name__=='__main__':
    try:main()
    except Exception as error:
        import traceback
        print(json.dumps({'authorized':False,'error_type':type(error).__name__,**OBSERVATION,
            'sites':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}))
        raise SystemExit(1)
