#!/usr/bin/env python3
"""Convert a CI-reviewed legacy compile-only archive, never execute a compiler.

This is a protected staging operation, NOT publication or approval. The caller
must review the deterministic index and receipt before adding a catalog entry.
Legacy native archives lack the v2 tool-binary/package hashes: do not relabel
them as v2 native hits. Go's ActionIDs still validate each Go compilation input.
"""
import argparse
import datetime as dt
import hashlib
from pathlib import Path
import re
import shutil
import tarfile
import time

import incremental_cache as c
import node_compile_cache as n


RUNTIME_FIELDS = ('tools', 'cpu', 'os', 'platform', 'go_env', 'workspace', 'gomodcache')
DISK_NAME = re.compile(r'gocache/([0-9a-f]{2})/([0-9a-f]{64})-([ad])')
HEADER = re.compile(rb'v1 ([0-9a-f]{64}) ([0-9a-f]{64}) ([ 0-9]{20}) ([ 0-9]{20})\n')


def reviewed_archive(directory, entry, current, api=n.legacy.api, now=None):
    """Revalidate pinned transport, manifest, archive and original producer."""
    directory = c.safe_path(directory)
    now = now or dt.datetime.now(dt.timezone.utc)
    p = entry['producer']
    c.require(p['repository'] == 'matrixorigin/matrixone' and
              p['event'] in ('push', 'workflow_dispatch'), 'untrusted legacy producer')
    run = api(f"repos/{p['repository']}/actions/runs/{p['id']}")
    artifact = api(f"repos/{p['repository']}/actions/artifacts/{entry['artifact']['id']}")
    n.legacy.validate_provenance(entry, run, artifact)
    c.require(artifact.get('expired') is False, 'legacy artifact expired or unknown')
    # Legacy catalogs pin artifact identity, not always expires_at. In that
    # case use the live API expiry, never fabricate an extended retention.
    expiries = []
    for value in (artifact['expires_at'], entry.get('expires_at', artifact['expires_at'])):
        expires = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        c.require(expires.tzinfo is not None and expires > now, 'legacy artifact expired')
        expiries.append(expires)
    transport = directory / 'transport.zip'
    c.require(transport.stat().st_size == artifact['size_in_bytes'] and
              'sha256:' + c.digest(transport) == artifact['digest'], 'legacy ZIP differs')
    path = directory / 'manifest.json'
    c.require(c.digest(path) == entry['manifest_sha256'], 'legacy manifest differs')
    m = c.read_json(path)
    c.require(m.get('compile_only') is True and m['fingerprint'] == entry['fingerprint'],
              'legacy cache not reviewed compile-only input')
    fp = m['fingerprint']
    c.require(current['schema'] == 2 and current['repository'] == entry['source_repository'] and
              current['environment'] == entry['environment'] and
              fp['source'] == entry['source'], 'legacy environment/repository/source differs')
    # Environment is pinned by the reviewed entry even for the original MNL2
    # schema which did not include cs_environment in its fingerprint.
    c.require(fp.get('cs_environment', entry['environment']) == current['environment'],
              'legacy fingerprint environment differs')
    c.require(all(fp[k] == current[k] for k in RUNTIME_FIELDS), 'legacy Go runtime incompatible')
    c.require(type(m['unpacked_bytes']) is int and 0 < m['unpacked_bytes'] <= 8*c.GIB and
              type(m['archive_bytes']) is int and 0 < m['archive_bytes'] <= 8*c.GIB,
              'legacy archive exceeds budget')
    archive = directory / 'build-cache.tar.gz'
    c.require(archive.stat().st_size == m['archive_bytes'] and
              c.digest(archive) == m['archive_sha256'], 'legacy archive differs')
    return archive, m, min(expiries).isoformat()


def scan(archive, manifest):
    """Validate all paths/native bytes and exact inventory before staging."""
    actions = {}; objects = {}; names = set(); native = set(); total = 0
    with c.safe_open(archive) as body, tarfile.open(fileobj=body, mode='r:gz') as tar:
        for member in tar:
            path = n.legacy.safe_relative(member.name)
            c.require(member.isfile() and member.name not in names and len(names) < 100000,
                      'legacy archive member type/duplicate/budget')
            c.require(0 <= member.size <= c.MAX_OBJECT, 'legacy member exceeds budget')
            names.add(member.name); total += member.size
            c.require(total <= manifest['unpacked_bytes'], 'legacy expanded budget')
            if path.parts[0] == 'native':
                name = '/'.join(path.parts[1:])
                c.require(n.legacy.native_allowed(name) and name in manifest['native_hashes'],
                          'unreviewed native member')
                digest = hashlib.sha256()
                with tar.extractfile(member) as src:
                    for block in iter(lambda: src.read(1024*1024), b''): digest.update(block)
                c.require(digest.hexdigest() == manifest['native_hashes'][name], 'legacy native bytes differ')
                native.add(name)
                continue
            if member.name in ('gocache/README', 'gocache/trim.txt'):
                c.require(member.size <= 4096, 'legacy housekeeping file too large')
                continue
            match = DISK_NAME.fullmatch(member.name)
            c.require(match and match[1] == match[2][:2], 'unsupported Go cache member')
            ident, kind = match[2], match[3]
            if kind == 'd':
                objects[ident] = member.size
                continue
            c.require(member.size == 175, 'unsupported Go cache action format')
            with tar.extractfile(member) as src: raw = src.read(176)
            header = HEADER.fullmatch(raw)
            c.require(header is not None and header[1].decode() == ident and int(header[4]) > 0,
                      'legacy action identity differs')
            record = {'output_id': header[2].decode(), 'size': int(header[3])}
            c.check_record(ident, record); actions[ident] = record
            c.require(len(actions) <= c.MAX_ACTIONS, 'legacy action budget')
    c.require(total == manifest['unpacked_bytes'] and native == set(manifest['native_hashes']),
              'legacy inventory differs')
    trimmed = 0
    for action, record in list(actions.items()):
        if record['output_id'] not in objects:
            del actions[action]; trimmed += 1
        else:
            c.require(objects[record['output_id']] == record['size'], 'legacy object size differs')
    return actions, trimmed


def migrate(directory, entry, current, candidate, api=n.legacy.api):
    started = time.monotonic()
    directory = c.safe_path(directory); candidate = c.safe_path(candidate)
    c.require(not candidate.exists() and directory not in candidate.parents and
              candidate not in directory.parents, 'migration must use fresh isolated stage')
    archive, manifest, expires = reviewed_archive(directory, entry, current, api)
    actions, trimmed = scan(archive, manifest)
    needed = {r['output_id']: r['size'] for r in actions.values()}
    c.require(shutil.disk_usage(candidate.parent).free >= sum(needed.values()) + 20*c.GIB,
              'migration disk reserve insufficient')
    # No native files, modules, test results, source files or credentials are
    # copied. A crash retains a private partial stage; it never publishes it.
    candidate.mkdir()
    with c.safe_open(archive) as body, tarfile.open(fileobj=body, mode='r:gz') as tar:
        for member in tar:
            match = DISK_NAME.fullmatch(member.name)
            if not match or match[3] != 'd' or match[2] not in needed: continue
            ident = match[2]
            with tar.extractfile(member) as src:
                c.atomic_file(candidate/'objects'/ident[:2]/ident, src, needed[ident], ident)
    index = {'schema': 2, 'runtime': current, 'actions': actions}
    c.atomic_json(candidate/'index.json', index)
    receipt = {'schema': 2, 'operation': 'legacy-go-format-conversion',
               'source_manifest_sha256': entry['manifest_sha256'],
               'source_archive_sha256': manifest['archive_sha256'],
               'source_artifact_id': entry['artifact']['id'], 'source_producer_id': entry['producer']['id'],
               'source_expires_at': expires,
               'index_sha256': c.digest(candidate/'index.json'), 'runtime': current,
               'action_count': len(actions), 'trimmed_action_count': trimmed,
               'object_count': len(needed), 'object_bytes': sum(needed.values()),
               'native_imported': False, 'compiler_invocations': 0,
               'seconds': round(time.monotonic()-started, 6)}
    c.atomic_json(candidate/'migration.json', receipt)
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('artifact', 'entry', 'runtime', 'candidate'):
        p.add_argument('--'+name, type=Path, required=True)
    a = p.parse_args()
    try:
        migrate(a.artifact, c.read_json(a.entry), c.read_json(a.runtime), a.candidate)
    except Exception:
        print('Legacy migration rejected; original PVC files retained.', file=__import__('sys').stderr)
        raise SystemExit(1)


if __name__ == '__main__': main()
