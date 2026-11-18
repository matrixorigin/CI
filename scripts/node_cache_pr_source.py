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


if __name__=='__main__':
    c.require(sys.argv[1:] == ['record'],'unknown PR provenance operation')
    source=Path(os.environ['GITHUB_WORKSPACE'])
    actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True,timeout=30).strip()
    value=checked(os.environ['ACTUAL_PR_SOURCE_REPOSITORY'],os.environ['ACTUAL_PR_HEAD_SHA'],actual,
        os.environ['CS_CACHE_SOURCE_REPOSITORY'])
    c.atomic_json(Path(os.environ['RUNNER_TEMP'])/'cs-node-cache/pr-source.json',value)
