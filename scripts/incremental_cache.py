#!/usr/bin/env python3
"""Go GOCACHEPROG: reviewed immutable node objects + a private writable job cache.

No SHA/group/pod name is a Go cache key here: Go supplies its input-derived
ActionID. Shared mappings come only from pinned, hash-verified indexes. No
network, publication, deletion or shared writes are performed by the protocol.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

GIB = 1024 ** 3
MAX_OBJECT = 512 * 1024 ** 2
MAX_INDEX = 32 * 1024 ** 2
MAX_ACTIONS = 100000
HEX = re.compile(r'[0-9a-f]{64}')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(path):
    h = hashlib.sha256()
    with safe_open(path) as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_path(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink in cache path')
    return path


def safe_open(path):
    path = safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    import stat
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('cache object is not regular')
    return os.fdopen(fd, 'rb')


def read_json(path, limit=MAX_INDEX):
    with safe_open(path) as f:
        require(os.fstat(f.fileno()).st_size <= limit, 'cache metadata too large')
        def unique(pairs):
            out = {}
            for key, value in pairs:
                require(key not in out, 'duplicate cache metadata key')
                out[key] = value
            return out
        return json.load(f, object_pairs_hook=unique)


def check_record(action, record):
    require(isinstance(action, str) and HEX.fullmatch(action), 'invalid action ID')
    require(set(record) == {'output_id', 'size'}, 'invalid action record')
    require(isinstance(record['output_id'], str) and HEX.fullmatch(record['output_id']), 'invalid output ID')
    require(type(record['size']) is int and 0 <= record['size'] <= MAX_OBJECT, 'invalid object size')


def atomic_file(path, source, expected_size, expected_hash, reserve=20 * GIB):
    """Private target only; no hard links back into a mutable job/shared tree."""
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(shutil.disk_usage(path.parent).free >= reserve + expected_size, 'cache disk reserve reached')
    if path.exists():
        require(path.stat().st_size == expected_size and digest(path) == expected_hash, 'existing object differs')
        return
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    tmp = Path(name)
    try:
        h, total = hashlib.sha256(), 0
        with os.fdopen(fd, 'wb') as out:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                total += len(block)
                require(total <= expected_size, 'oversized cache object')
                h.update(block)
                out.write(block)
            require(total == expected_size and h.hexdigest() == expected_hash, 'cache object digest mismatch')
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o444)
        # Link publishes without overwriting a concurrent writer. Only this
        # job's private temporary inode is linked, never a shared cache object.
        try:
            os.link(tmp, path)
        except FileExistsError:
            require(path.stat().st_size == expected_size and digest(path) == expected_hash, 'concurrent object differs')
    finally:
        tmp.unlink(missing_ok=True)  # only the unique temporary file created above


def atomic_json(path, value):
    import io
    raw = canonical(value)
    atomic_file(path, io.BytesIO(raw), len(raw), hashlib.sha256(raw).hexdigest(), reserve=0)


class Cache:
    def __init__(self, config):
        require(config['schema'] == 2, 'unsupported cache configuration')
        self.local = safe_path(config['local'])
        self.shared = safe_path(config['shared']) if config.get('shared') else None
        require(self.local.is_absolute(), 'private cache path must be absolute')
        if self.shared:
            require(self.local != self.shared and self.shared not in self.local.parents
                    and self.local not in self.shared.parents, 'private/shared cache overlap')
        self.local.mkdir(parents=True, exist_ok=True)
        self.reserve = config.get('reserve_bytes', 20 * GIB)
        require(type(self.reserve) is int and self.reserve >= 0, 'invalid disk reserve')
        self.index = {}
        self.stats = {'shared_hits':0, 'local_hits':0, 'misses':0, 'puts':0, 'copied_bytes':0}
        require(not (self.local/'validation-failed.json').exists(), 'previous cache validation failed')
        self.started = time.monotonic()
        require(len(config['indexes']) <= 16, 'too many cache layers')
        for index_id in config['indexes']:
            require(self.shared is not None and HEX.fullmatch(index_id), 'invalid trusted index ID')
            path = self.shared/'indexes'/(index_id+'.json')
            require(digest(path) == index_id, 'reviewed cache index digest mismatch')
            index = read_json(path)
            require(index['schema'] == 2 and index['runtime'] == config['runtime'], 'cache runtime mismatch')
            require(len(index['actions']) <= MAX_ACTIONS, 'too many actions')
            for action, record in index['actions'].items():
                check_record(action, record)
                require(action not in self.index or self.index[action] == record, 'conflicting trusted action mapping')
                self.index[action] = record
        require(len(self.index) <= MAX_ACTIONS, 'too many combined actions')

    def object_path(self, root, output):
        require(HEX.fullmatch(output), 'invalid object address')
        return root/'objects'/output[:2]/output

    def get(self, action):
        require(HEX.fullmatch(action), 'invalid action address')
        mapping = self.local/'actions'/action[:2]/(action+'.json')
        record = read_json(mapping, 4096) if mapping.exists() else self.index.get(action)
        if record is None:
            self.stats['misses'] += 1
            return {'Miss':True}
        check_record(action, record)
        target = self.object_path(self.local, record['output_id'])
        if mapping.exists():
            require(target.stat().st_size == record['size'] and digest(target) == record['output_id'], 'private object corrupted')
            self.stats['local_hits'] += 1
        else:
            source = self.object_path(self.shared, record['output_id'])
            with safe_open(source) as f:
                require(os.fstat(f.fileno()).st_size == record['size'], 'shared object size mismatch')
                atomic_file(target, f, record['size'], record['output_id'], self.reserve)
            atomic_json(mapping, record)
            self.stats['shared_hits'] += 1
            self.stats['copied_bytes'] += record['size']
        return {'OutputID':base64.b64encode(bytes.fromhex(record['output_id'])).decode(),
                'Size':record['size'], 'DiskPath':str(target)}

    def put(self, action, output, size, body):
        record = {'output_id':output, 'size':size}
        check_record(action, record)
        require(action not in self.index or self.index[action] == record, 'new build disagrees with trusted action')
        target = self.object_path(self.local, output)
        atomic_file(target, body, size, output, self.reserve)
        atomic_json(self.local/'actions'/action[:2]/(action+'.json'), record)
        self.stats['puts'] += 1
        return {'OutputID':base64.b64encode(bytes.fromhex(output)).decode(), 'Size':size, 'DiskPath':str(target)}

    def close(self):
        self.stats['seconds'] = round(time.monotonic()-self.started, 6)
        atomic_json(self.local/'stats'/f'{os.getpid()}-{time.time_ns()}.json', self.stats)


def id_hex(value):
    raw = base64.b64decode(value, validate=True)
    require(len(raw) == 32, 'cache ID must be 32 bytes')
    return raw.hex()


def protocol(config, stdin, stdout):
    cache = Cache(config)
    def reply(value):
        stdout.write(canonical(value)+b'\n')
        stdout.flush()
    reply({'KnownCommands':['get','put','close']})
    try:
        for line in iter(lambda: stdin.readline(65537), b''):
            require(len(line) <= 65536 and line.endswith(b'\n'), 'invalid protocol line')
            if not line.strip():
                continue
            req = json.loads(line)
            require(type(req['ID']) is int, 'invalid request ID')
            command = req['Command']
            if command == 'get':
                result = cache.get(id_hex(req['ActionID']))
            elif command == 'put':
                size = req.get('BodySize', 0)
                require(type(size) is int and 0 <= size <= MAX_OBJECT, 'object exceeds budget')
                require(shutil.disk_usage(cache.local).free >= cache.reserve + 2*size, 'cache disk reserve reached')
                # Stream the protocol's quoted base64 body into a private
                # temporary file; memory stays bounded even for big archives.
                with tempfile.TemporaryFile(dir=cache.local) as body:
                    if size:
                        first=stdin.read(1)
                        while first in (b'\n',b'\r',b' ',b'\t'):
                            first=stdin.read(1)
                        require(first == b'"', 'invalid object body')
                        remaining = 4*((size+2)//3)
                        while remaining:
                            block = stdin.read(min(1024*1024, remaining))
                            require(block and len(block)%4 == 0, 'truncated object body')
                            body.write(base64.b64decode(block, validate=True))
                            remaining -= len(block)
                        require(stdin.read(2) == b'"\n', 'invalid object body terminator')
                    require(body.tell() == size, 'object size differs')
                    body.seek(0)
                    result = cache.put(id_hex(req['ActionID']), id_hex(req['OutputID']), size, body)
            elif command == 'close':
                cache.close()
                reply({'ID':req['ID']})
                return
            else:
                raise ValueError('unknown cache command')
            reply({'ID':req['ID'], **result})
    except Exception as exc:
        # Go may treat an individual cache Get error as a miss. A durable
        # failure marker is also checked by the workflow before acceptance.
        atomic_json(cache.local/'validation-failed.json', {'invalid':True})
        print('cache protocol rejected: '+type(exc).__name__,file=sys.stderr)
        raise


def export_index(local, runtime, output):
    """Create a candidate index only. This does NOT authorize publication.

    Trusted publisher must verify successful workflow/source provenance before
    making any candidate objects available to other jobs. PR jobs never publish.
    """
    local = safe_path(local)
    require(not (local/'validation-failed.json').exists(), 'cache validation failed')
    actions = {}
    for path in sorted((local/'actions').glob('*/*.json')):
        action = path.stem
        record = read_json(path,4096)
        check_record(action,record)
        obj = local/'objects'/record['output_id'][:2]/record['output_id']
        require(obj.stat().st_size == record['size'] and digest(obj) == record['output_id'], 'candidate object corrupted')
        actions[action] = record
        require(len(actions) <= MAX_ACTIONS, 'too many candidate actions')
    index = {'schema':2, 'runtime':runtime, 'actions':actions}
    atomic_json(output,index)
    return digest(output)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    config=read_json(args.config)
    try:
        protocol(config,sys.stdin.buffer,sys.stdout.buffer)
    except Exception:
        # No exception bodies/paths/credentials enter protocol output or logs.
        print('incremental cache validation failed',file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':main()
