#!/usr/bin/env python3
"""Fixed-input race measurement. No rollout decision is automated here."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/home/runner/_work/matrixone/matrixone')
CACHE = Path('/home/runner/.cache/mo-race-canary')
MODULES = Path('/home/runner/go/pkg/mod')


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def command(args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=120).strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def execution(path):
    """Compare execution multisets, not just a headline passing-test count."""
    runs, ends, outcomes = Counter(), Counter(), Counter()
    packages, completed = Counter(), Counter()
    with path.open() as stream:
        for line in stream:
            if not line.startswith('{'):
                continue
            event = json.loads(line)  # truncated JSON must not look like success
            package, test = event.get('Package', ''), event.get('Test', '')
            action = event.get('Action')
            if action == 'start':
                packages[package] += 1
            if action == 'run' and test:
                runs[(package, test)] += 1
            if action in ('pass', 'skip', 'fail'):
                outcomes[(package, test, action)] += 1
                if test:
                    ends[(package, test)] += 1
                else:
                    completed[package] += 1
    if not runs or runs != ends or any(k[2] == 'fail' for k in outcomes):
        raise ValueError('empty, failed or incomplete test execution')
    if not packages or packages != completed:
        raise ValueError('missing package start or completion')
    if any(p not in packages for p, test in runs):
        raise ValueError('test without package start')
    rows = sorted([*key, count] for key, count in outcomes.items())
    return {'test_runs': sum(runs.values()),
            'outcome_sha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            'counts': dict(Counter({kind: sum(n for (p, t, a), n in outcomes.items()
                                             if a == kind and t)
                                    for kind in ('pass', 'skip', 'fail')})),
            'outcomes': rows}


def fingerprint():
    cpu = Path('/proc/cpuinfo').read_text()
    mem = Path('/proc/meminfo').read_text()
    return {
        'source_sha': command(['git', 'rev-parse', 'HEAD'], SOURCE),
        'ci_sha': command(['git', 'rev-parse', 'HEAD'], ROOT),
        'image': os.environ['SEED_IMAGE'],
        'runner_image': [os.environ.get(k, '') for k in ('ImageOS', 'ImageVersion')],
        'runner_label': os.environ['CANARY_RUNNER_LABEL'],
        'cpu_model': sorted(set(re.findall(r'^model name\s*:\s*(.*)', cpu, re.M))),
        'cpu_count': os.cpu_count(),
        'mem_total': re.search(r'^MemTotal:.*', mem, re.M)[0],
        'go': json.loads(command(['go', 'env', '-json', 'GOVERSION', 'GOOS', 'GOARCH',
                                  'GOAMD64', 'GOEXPERIMENT', 'CGO_ENABLED', 'GOFLAGS',
                                  'GOMOD', 'GOMODCACHE', 'GOPROXY'], SOURCE)),
        'compiler': command(['cc', '--version']),
        'cmake': command(['cmake', '--version']),
        'kernel': command(['uname', '-srmo']),
        'ut_parallel': os.environ['CANARY_UT_PARALLEL'],
        'ut_timeout': os.environ['CANARY_UT_TIMEOUT'],
    }


def sample():
    result = {'monotonic': time.monotonic(), 'disk_free': shutil.disk_usage(SOURCE).free}
    for name, file in {
        'cpu': '/proc/stat', 'memory': '/proc/meminfo', 'disk': '/proc/diskstats',
        'pressure_io': '/proc/pressure/io', 'pressure_cpu': '/proc/pressure/cpu',
        'cgroup_cpu': '/sys/fs/cgroup/cpu.stat',
        'cgroup_memory': '/sys/fs/cgroup/memory.current',
        'cgroup_memory_events': '/sys/fs/cgroup/memory.events',
        'cgroup_io': '/sys/fs/cgroup/io.stat',
    }.items():
        try:
            result[name] = Path(file).read_text()
        except FileNotFoundError:
            result[name] = None
    return result


def measured_command(args, log, timeout):
    """Bound the process group, preserve nonzero status and capture CPU cost."""
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with log.open('wb') as output:
        child = subprocess.Popen(args, cwd=SOURCE, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = child.wait(timeout=timeout)
        except BaseException:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
            raise
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = {'seconds': time.monotonic() - started, 'exit_code': code,
              'user_seconds': after.ru_utime - before.ru_utime,
              'system_seconds': after.ru_stime - before.ru_stime}
    return result


def resource_summary(path):
    with path.open() as stream:
        samples = [json.loads(line) for line in stream]
    if len(samples) < 2:
        raise ValueError('missing resource samples')
    def cpu(row):
        return list(map(int, row['cpu'].splitlines()[0].split()[1:9]))
    first, last = cpu(samples[0]), cpu(samples[-1])
    delta = [b - a for a, b in zip(first, last)]
    ticks = sum(delta)
    busy = []
    for a, b in zip(samples, samples[1:]):
        d = [y - x for x, y in zip(cpu(a), cpu(b))]
        if sum(d) > 0:
            busy.append(100 * (1 - (d[3] + d[4]) / sum(d)))
    mem = [int(re.search(r'^MemAvailable:\s+(\d+)', r['memory'], re.M)[1]) * 1024
           for r in samples]
    return {'host_cpu_busy_percent': 100 * (1 - (delta[3] + delta[4]) / ticks) if ticks else 0,
            'host_cpu_peak_interval_percent': max(busy, default=0),
            'host_cpu_seconds': (ticks - delta[3] - delta[4]) / os.sysconf('SC_CLK_TCK'),
            'min_memory_available_bytes': min(mem),
            'min_disk_free_bytes': min(r['disk_free'] for r in samples),
            'sample_interval_seconds': 5,
            'note': 'Host-wide sampled counters; daemon CPU included, short peaks may be missed'}


def prepare(snapshot):
    if os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise ValueError('canary requires a fresh GitHub-hosted runner')
    for path in (SOURCE, CACHE, MODULES):
        if path.exists() or path.is_symlink():
            raise ValueError(f'preexisting path; refusing to alter it: {path}')
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ROOT / 'subject'), SOURCE)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    MODULES.parent.mkdir(parents=True, exist_ok=True)
    initial = {'cache_state': 'cold', 'snapshot_sha256': None}
    if snapshot:
        meta = json.loads((snapshot / 'snapshot.json').read_text())
        archive = snapshot / 'cache.tar'
        if sha256(archive) != meta['sha256']:
            raise ValueError('snapshot hash mismatch')
        staging = snapshot / 'extracted'
        staging.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(staging, filter='data')
        shutil.move(str(staging / 'go-build'), CACHE)
        shutil.move(str(staging / 'mod'), MODULES)
        if meta['identity'] != fingerprint():
            raise ValueError('snapshot environment mismatch')
        archive.unlink()  # only the task-owned downloaded archive
        initial = {'cache_state': 'warm', 'snapshot_sha256': meta['sha256']}
    else:
        CACHE.mkdir()
        MODULES.mkdir()
    if (CACHE / '.matrixone-seed.json').exists():
        raise ValueError('initial compiler cache must not contain a seed marker')
    # Identical test-result invalidation in A/B; compiler cache is preserved.
    command(['go', 'clean', '-testcache'], SOURCE)
    return initial


def run(args):
    out = ROOT / 'canary-report'
    out.mkdir(exist_ok=True)
    report = {'valid': False, 'seed_enabled': args.seed, 'phases': {}}
    stop = threading.Event()

    def monitor():
        with (out / 'resources.jsonl').open('w') as log:
            while True:
                log.write(json.dumps(sample()) + '\n')
                log.flush()
                if stop.wait(5):
                    break

    monitor_thread = None
    try:
        if not all(os.environ.get(k) for k in ('endpoint', 'region', 'apikey', 'apisecret', 'bucket')):
            raise ValueError('CI S3 test credentials must be configured; no secret values are logged')
        report['initial'] = prepare(args.snapshot)
        report['identity'] = fingerprint()
        before_images = command(['docker', 'image', 'ls', '-q', '--no-trunc'])
        report['initial_images'] = sorted(before_images.splitlines())
        # The pinned builder cannot already be local, even for warm Go caches.
        if subprocess.run(['docker', 'image', 'inspect', os.environ['SEED_IMAGE']],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=30).returncode == 0:
            raise ValueError('builder image already present')
        report['disk_before'] = command(['df', '-Pk', str(SOURCE), str(CACHE), str(MODULES)])
        measured_start = time.monotonic()
        monitor_thread = threading.Thread(target=monitor)
        monitor_thread.start()
        try:
            if args.seed:
                phase = measured_command([sys.executable, str(ROOT / 'actions/seed-go-caches/seed.py')],
                                         out / 'seed.log', 1140)
                report['phases']['seed'] = phase
                report['seed'] = json.loads((out / 'seed.log').read_text().splitlines()[-1])
            for name, cmd, timeout in [
                ('clean', ['make', 'clean'], 300),
                ('config', ['make', 'config'], 900),
                ('ut', ['make', 'ut', 'UT_CONFIGURED=1', 'UT_SHARD=all',
                        'UT_PARALLEL=' + os.environ['CANARY_UT_PARALLEL'],
                        'UT_TIMEOUT=' + os.environ['CANARY_UT_TIMEOUT']], 4500),
            ]:
                report['phases'][name] = measured_command(cmd, out / (name + '.log'), timeout)
                if report['phases'][name]['exit_code']:
                    raise ValueError(name + ' failed')
        finally:
            report['total_seconds'] = time.monotonic() - measured_start
            stop.set()
            monitor_thread.join(timeout=10)
            with (out / 'resources.jsonl').open('a') as log:
                log.write(json.dumps(sample()) + '\n')
            report['resources'] = resource_summary(out / 'resources.jsonl')
        report['disk_after'] = command(['df', '-Pk', str(SOURCE), str(CACHE), str(MODULES)])
        report['docker_disk'] = command(['docker', 'system', 'df'])
        reports = list((SOURCE / 'scratch').rglob('*-UT-Report.out'))
        if len(reports) != 1:
            raise ValueError('expected exactly one complete raw UT report')
        report['execution'] = execution(reports[0])
        if args.seed:
            seeded = report['seed']
            if (seeded.get('state') != 'seeded' or seeded.get('producer_flavor_status') != 'ok'
                    or seeded.get('cleanup') != 'complete' or seeded.get('imported_files', 0) <= 0):
                raise ValueError('seed skipped, partial, empty or failed: invalid B sample')
        report['valid'] = True
        if args.export:
            args.export.mkdir()
            archive = args.export / 'cache.tar'
            with tarfile.open(archive, 'w') as stream:
                stream.add(CACHE, arcname='go-build')
                stream.add(MODULES, arcname='mod')
            dump(args.export / 'snapshot.json', {'identity': report['identity'], 'sha256': sha256(archive)})
    except Exception as error:
        report.update(valid=False, error=str(error))
    finally:
        stop.set()
        if monitor_thread:
            monitor_thread.join(timeout=10)
        dump(out / 'result.json', report)
        print(json.dumps({k: v for k, v in report.items() if k != 'execution'}, indent=2), flush=True)
    return 0 if report['valid'] else 1


def compare(a, b):
    if not a.get('valid') or not b.get('valid'):
        raise ValueError('invalid/incomplete arm')
    if a['seed_enabled'] or not b['seed_enabled']:
        raise ValueError('expected A=off, B=on')
    for key in ('identity', 'initial', 'initial_images', 'execution'):
        if a[key] != b[key]:
            raise ValueError('pair mismatch: ' + key)
    return {'A_seconds': a['total_seconds'], 'B_seconds': b['total_seconds'],
            'saved_seconds': a['total_seconds'] - b['total_seconds'],
            'saved_percent': 100 * (1 - b['total_seconds'] / a['total_seconds'])}


def summarize(directory):
    results = []
    for path in sorted(directory.glob('race-*-A/result.json')):
        other = path.parent.with_name(path.parent.name[:-1] + 'B') / 'result.json'
        if not other.exists():
            raise ValueError('missing B: ' + str(other))
        result = compare(json.loads(path.read_text()), json.loads(other.read_text()))
        results.append({'pair': path.parent.name[:-2], **result})
    expected = 2 * int(os.environ['CANARY_REPETITIONS'])
    if len(results) != expected:
        raise ValueError(f'expected {expected} cold/warm pairs; got {len(results)}')
    dump(directory / 'comparison.json', results)
    text = '| Pair | A seconds | B seconds | Saved seconds | Saved % |\n|---|---:|---:|---:|---:|\n'
    for r in results:
        text += f"| {r['pair']} | {r['A_seconds']:.1f} | {r['B_seconds']:.1f} | {r['saved_seconds']:.1f} | {r['saved_percent']:.1f} |\n"
    text += '\nValid paired execution only. Review raw resource samples before any rollout decision.\n'
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', action='store_true')
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--export', type=Path)
    parser.add_argument('--summarize', type=Path)
    options = parser.parse_args()
    if options.summarize:
        summarize(options.summarize)
    else:
        def cancelled(signum, frame):
            raise InterruptedError(f'cancelled by signal {signum}')
        signal.signal(signal.SIGTERM, cancelled)
        sys.exit(run(options))
