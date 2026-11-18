#!/usr/bin/env python3
"""Authenticate an approved candidate ZIP and stage it privately, never publish.

The protected caller supplies the immutable reviewed entry. Ordinary test jobs
must not select their own approval. No cluster writes, compiler or shared-cache
writes occur here. Partial transfers are retained, not overwritten or retried.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import incremental_cache as c
import node_compile_cache as n
import node_cache_bundle as bundle


def signed_archive_url(reviewed):
    # The only credential-bearing request is to the fixed GitHub API origin.
    token = os.environ.get('CS_CACHE_READ_TOKEN', '')
    c.require(bool(token), 'artifact read credential unavailable')
    ident = reviewed['artifact']['id']
    c.require(type(ident) is int and ident > 0, 'invalid artifact ID')
    req = urllib.request.Request(
        f'https://api.github.com/repos/matrixorigin/matrixone/actions/artifacts/{ident}/zip',
        headers={'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=30)
    except urllib.error.HTTPError as error:
        c.require(error.code == 302, 'artifact redirect unavailable')
        url = error.headers['Location']
    else:
        raise ValueError('expected artifact redirect')
    target = urllib.parse.urlsplit(url)
    c.require(target.scheme == 'https' and not target.username and not target.password and
              target.port in (None, 443) and target.hostname and
              (target.hostname.endswith('.blob.core.windows.net') or
               target.hostname.endswith('.githubusercontent.com')), 'unexpected artifact origin')
    return url


def unpack_verified_transport(directory, reviewed):
    directory = c.safe_path(directory); transport = directory/'transport.zip'
    artifact = reviewed['artifact']
    c.require(transport.stat().st_size == artifact['size_in_bytes'] and
              'sha256:'+c.digest(transport) == artifact['digest'], 'artifact ZIP differs')
    with c.safe_open(transport) as body, zipfile.ZipFile(body) as archive:
        members = archive.infolist()
        c.require(len(members) == 3 and {m.filename for m in members} ==
                  {'manifest.json', 'index.json', 'delta.tar.gz'}, 'unexpected ZIP inventory')
        for member in members:
            mode = member.external_attr >> 16
            c.require(not member.is_dir() and not member.flag_bits & 1 and
                      mode & 0o170000 in (0, 0o100000), 'unsupported ZIP member')
        manifest_info = archive.getinfo('manifest.json')
        c.require(manifest_info.file_size <= c.MAX_INDEX, 'manifest too large')
        raw = archive.read(manifest_info)
        c.require(hashlib.sha256(raw).hexdigest() == reviewed['manifest_sha256'], 'manifest differs')
        import io
        c.atomic_file(directory/'manifest.json', io.BytesIO(raw), len(raw), reviewed['manifest_sha256'])
        m = c.read_json(directory/'manifest.json')
        c.require(m['schema'] == 2 and m['compile_only'] is True and m['runtime'] == reviewed['runtime']
                  and m['index_sha256'] == reviewed['index_sha256'], 'manifest metadata differs')
        expected = {'index.json': (None, reviewed['index_sha256']),
                    'delta.tar.gz': (m['archive_bytes'], m['archive_sha256'])}
        for name, (size, digest) in expected.items():
            member = archive.getinfo(name)
            limit = c.MAX_INDEX if name == 'index.json' else 8*c.GIB
            c.require(0 < member.file_size <= limit and (size is None or member.file_size == size),
                      'ZIP member size differs')
            with archive.open(member) as src:
                c.atomic_file(directory/name, src, member.file_size, digest)
    return m


def receive(reviewed, directory, candidate, api=n.legacy.api, url_factory=signed_archive_url,
            open_url=urllib.request.urlopen,unpack=unpack_verified_transport):
    directory = c.safe_path(directory); candidate = c.safe_path(candidate)
    c.require(not directory.exists() and not candidate.exists() and directory != candidate and
              directory not in candidate.parents and candidate not in directory.parents,
              'fresh disjoint private paths required')
    c.require(n.reviewed_entries({'entries': [reviewed]}, reviewed['runtime'], api) == [reviewed],
              'approved candidate unavailable or expired')
    size = reviewed['artifact']['size_in_bytes']
    c.require(type(size) is int and 0 < size <= 8*c.GIB and
              shutil.disk_usage(directory.parent).free >= size+20*c.GIB, 'transport reserve')
    url = url_factory(reviewed)  # do not log URL, token, headers or exception messages
    directory.mkdir()
    partial = directory/'transport.zip.partial'
    total = 0; sha = hashlib.sha256()
    # No Authorization header is carried to the signed storage origin.
    with open_url(url, timeout=60) as response, partial.open('xb') as output:
        c.require(response.status == 200 and int(response.headers['Content-Length']) == size,
                  'artifact response size differs')
        while True:
            block = response.read(1024*1024)
            if not block: break
            total += len(block)
            c.require(total <= size and shutil.disk_usage(directory).free >= len(block)+20*c.GIB,
                      'transport size or reserve exceeded')
            output.write(block); sha.update(block)
        output.flush(); os.fsync(output.fileno())
    c.require(total == size and 'sha256:'+sha.hexdigest() == reviewed['artifact']['digest'],
              'artifact transfer incomplete or corrupted')
    # Unique fresh directory: publish only this verified transfer, not an old partial.
    os.link(partial, directory/'transport.zip')
    partial.unlink()
    unpack(directory, reviewed)
    return bundle.stage(directory, reviewed, candidate, api)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('reviewed-entry','directory','candidate'): p.add_argument('--'+name, type=Path, required=True)
    a = p.parse_args()
    try: receive(c.read_json(a.reviewed_entry), a.directory, a.candidate)
    except Exception as error:
        print(json.dumps({'success':False,'error_type':type(error).__name__,
                          'shared_cache_modified':False,'automatic_retry':False}))
        raise SystemExit(1)


if __name__ == '__main__': main()
