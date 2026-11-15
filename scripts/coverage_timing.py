#!/usr/bin/env python3
"""Extract per-package and embedded-cluster admission timings from Go JSON."""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

UNIT = {'ns': 1e-9, 'µs': 1e-6, 'us': 1e-6, 'ms': 1e-3, 's': 1, 'm': 60, 'h': 3600}
PART = re.compile(r'(\d+(?:\.\d+)?)(ns|µs|us|ms|s|m|h)')


def seconds(value):
    parts = list(PART.finditer(value))
    if not parts or ''.join(m.group(0) for m in parts) != value:
        raise ValueError('unrecognized Go duration: ' + value)
    return sum(float(m.group(1)) * UNIT[m.group(2)] for m in parts)


def summarize(path):
    packages, waits = {}, defaultdict(list)
    parse_errors = 0
    with path.open(encoding='utf-8') as source:
        for line in source:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                parse_errors += 1
                continue
            package = event.get('Package')
            if not package:
                continue
            if event.get('Action') in ('pass', 'fail', 'skip') and not event.get('Test'):
                packages[package] = {'action': event['Action'], 'elapsed_seconds': event.get('Elapsed')}
            output = event.get('Output', '')
            if 'MO_UT_SETUP' in output and 'phase=admission-acquire' in output:
                found = re.search(r'(?:^|\s)duration=([^\s]+)', output)
                if found:
                    try:
                        waits[package].append({'test': event.get('Test'), 'seconds': seconds(found.group(1))})
                    except ValueError:
                        parse_errors += 1
    return {'packages': packages, 'admission_observations': dict(waits), 'parse_errors': parse_errors,
            'limitations': 'Admission values cover only emitted embedded-cluster setup records; package times overlap.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('report', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(summarize(args.report), out, ensure_ascii=False, indent=2)
        out.write('\n')
