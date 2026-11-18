"""Record actual trusted PR fork/head independently of cache namespace."""
import os
from pathlib import Path
import re
import subprocess
import sys

import incremental_cache as c


def checked(repository,head,actual,project='matrixorigin/matrixone'):
    c.require(project=='matrixorigin/matrixone' and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository)
        and re.fullmatch('[0-9a-f]{40}',head) and head==actual,
        'actual PR source identity differs from trusted checkout')
    return {'schema':1,'project_cache_namespace':project,'actual_pr_repository':repository,
        'actual_pr_head_sha':head,'checked_out_head_sha':actual,'shared_cache_writes':0}


def provenance(mode,source,temp,repository,head,project):
    c.require(mode in ('check','record'),'unknown PR provenance operation')
    source=Path(source);temp=Path(temp)
    actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True,timeout=30).strip()
    value=checked(repository,head,actual,project)
    if mode=='record':
        directory=temp/'cs-node-cache';target=directory/'pr-source.json'
        state=c.read_json(directory/'state.json')
        c.require(state['source']==str(source) and state['config']['runtime']['repository']==project,
            'PR provenance does not match the configured private cache source')
        c.require(not target.exists(),'PR provenance already exists; do not overwrite')
        c.atomic_json(target,value)
    # check MUST NOT create the per-job cache directory. configure owns its
    # exclusive first creation, preserving the original fresh-cache invariant.
    return value


if __name__=='__main__':
    c.require(len(sys.argv)==2,'unknown PR provenance operation')
    print(c.canonical(provenance(sys.argv[1],os.environ['GITHUB_WORKSPACE'],os.environ['RUNNER_TEMP'],
        os.environ['ACTUAL_PR_SOURCE_REPOSITORY'],os.environ['ACTUAL_PR_HEAD_SHA'],os.environ['CS_CACHE_SOURCE_REPOSITORY'])).decode())
