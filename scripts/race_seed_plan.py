#!/usr/bin/env python3
"""Validate immutable inputs before admitting credentialed race jobs."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess


def matrix(repetitions):
    if repetitions not in (1, 3):
        raise ValueError('repetitions must be 1 or 3')
    rows = []
    for repeat in range(1, repetitions + 1):
        for state in ('cold', 'warm'):
            for arm in ('AB' if repeat % 2 else 'BA'):
                rows.append(dict(name=f'race-{state}-{repeat}-{arm}',
                                 cache_state=state, seed=arm == 'B'))
    return {'include': rows}


if __name__ == '__main__':
    sha = os.environ['SOURCE_SHA']
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('full source SHA required')
    spec = importlib.util.spec_from_file_location('seed', Path(__file__).resolve().parents[1]
                                                 / 'actions/seed-go-caches/seed.py')
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    if not os.environ['SEED_IMAGE']:
        raise ValueError('pinned image is required')
    seed.image_candidates(os.environ['SEED_IMAGE'])
    status = subprocess.check_output(['gh', 'api',
        f'repos/matrixorigin/matrixone/compare/{sha}...main', '--jq', '.status'],
        text=True, timeout=60).strip()
    if status not in ('ahead', 'identical'):
        raise ValueError('source SHA is not reachable from official main')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('matrix=' + json.dumps(matrix(int(os.environ['REPETITIONS']))) + '\n')
