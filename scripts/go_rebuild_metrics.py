"""Reduce Go's job-private action graph; never publish raw command lines."""
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path


def summarize(graph, tool_dir):
    if not isinstance(graph, list) or not graph or not os.path.isabs(tool_dir):
        raise ValueError('missing action graph or exact tool directory')
    ids = set()
    packages = set()
    invocations = 0
    build_actions = 0
    for action in graph:
        ident = action['ID']
        if type(ident) is not int or ident in ids:
            raise ValueError('invalid action identity')
        ids.add(ident)
        commands = action['Cmd']
        if not isinstance(commands, (list, type(None))) or action.get('Failed'):
            raise ValueError('incomplete action evidence')
        if action['Mode'] == 'build':
            build_actions += 1
        for cmd in commands or ():
            argv = shlex.split(cmd)
            if not argv or Path(argv[0]).name != 'compile':
                continue
            if argv[0] != str(Path(tool_dir) / 'compile'):
                raise ValueError('unexpected Go compiler path')
            if action['Mode'] != 'build' or not action.get('Package') or '-p' not in argv:
                raise ValueError('unclassified compiler action')
            invocations += 1
            packages.add(hashlib.sha256(action['Package'].encode()).hexdigest())
    if not build_actions or not any(a.get('Cmd') for a in graph):
        raise ValueError('unobserved or empty build')
    return dict(schema=1, scope='go-test-group', go_compiler_invocations=invocations,
                distinct_action_package_count=len(packages), package_hashes=sorted(packages))


def run_measured(command, *, diagnostics, output):
    """Opt-in from the pinned CI driver only. Do not use -a or -toolexec."""
    output=Path(output)
    if output.exists() or command[:2]!=['go','test'] or '-count=1' not in command:
        raise ValueError('unexpected test command or non-fresh summary')
    if any(arg.startswith(('-debug-actiongraph','-toolexec')) or arg=='-a' for arg in command):
        raise ValueError('unexpected compiler instrumentation')
    tool_dir=subprocess.check_output(['go','env','GOTOOLDIR'],text=True).strip()
    if not (Path(tool_dir)/'compile').is_file() or not Path(tool_dir).is_absolute():
        raise ValueError('Go compiler missing')
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.go-action-') as private:
        graph=Path(private)/'actions.json'
        result=subprocess.run(command[:2]+[f'-debug-actiongraph={graph}']+command[2:],stderr=diagnostics)
        if result.returncode:
            return result  # original UT failure remains the result
        if not graph.is_file() or graph.is_symlink() or not 0<graph.stat().st_size<=128*1024*1024:
            raise ValueError('missing or oversized action graph')
        summary=summarize(json.loads(graph.read_bytes()),tool_dir)
        with output.open('x') as target:
            json.dump(summary,target,sort_keys=True)
    return result
