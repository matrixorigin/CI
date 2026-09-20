"""COPY-layer transport contracts; no network or daemon required."""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
from unittest import mock
import unittest

import registry
from test_seed import tar_bytes, MISSING, seed


def image_fixture():
    blobs = {}
    layers, diff_ids = [], []
    for _, path in registry.COPY_PATHS:
        raw = tar_bytes([(path.lstrip('/'), b'{}')])
        compressed = gzip.compress(raw)
        desc = dict(digest=registry.digest(compressed), size=len(compressed),
                    mediaType='application/vnd.oci.image.layer.v1.tar+gzip')
        blobs[desc['digest']] = compressed
        layers.append(desc)
        diff_ids.append(registry.digest(raw))
    config = dict(os='linux', architecture='amd64', rootfs={'diff_ids': diff_ids},
                  history=[{'created_by': f'COPY {src} {dst} # buildkit'} for src, dst in registry.COPY_PATHS])
    raw_config = json.dumps(config).encode()
    config_desc = dict(digest=registry.digest(raw_config), size=len(raw_config))
    blobs[config_desc['digest']] = raw_config
    manifest = dict(schemaVersion=2, config=config_desc, layers=layers)
    raw_manifest = json.dumps(manifest).encode()
    reference = 'matrixorigin/matrixone@' + registry.digest(raw_manifest)
    return manifest, config, raw_manifest, blobs, reference


class RegistryTests(unittest.TestCase):
    def test_layout_rejects_drift_platform_compression_count_and_size(self):
        manifest, config, *_ = image_fixture()
        self.assertEqual(len(registry.layout(manifest, config)), 6)
        for field, value in [('architecture', 'arm64'), ('history', []), ('rootfs', {})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                registry.layout(manifest, dict(config, **{field: value}))
        for field, value in [('mediaType', 'zstd'), ('size', registry.BLOB_LIMIT + 1), ('digest', 'bad')]:
            changed = copy.deepcopy(manifest)
            changed['layers'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                registry.layout(changed, config)
        changed = copy.deepcopy(config)
        changed['history'][-1]['created_by'] = 'RUN mutate cached files'
        with self.assertRaises(ValueError):
            registry.layout(manifest, changed)

    def test_digest_reader_hashes_trailing_bytes_and_enforces_budget(self):
        raw = tar_bytes([]) + b'trailing bytes'
        reader = registry.DigestReader(io.BytesIO(raw), len(raw))
        with tarfile.open(fileobj=reader, mode='r|') as archive:
            self.assertEqual(list(archive), [])
        reader.finish(registry.digest(raw))
        self.assertEqual(reader.size, len(raw))
        with self.assertRaises(ValueError):
            registry.DigestReader(io.BytesIO(raw), 1).finish(registry.digest(raw))
        with self.assertRaises(ValueError):
            registry.DigestReader(io.BytesIO(raw), len(raw)).finish('sha256:' + '0' * 64)

    def test_strict_direct_layer_extract_paths_whiteout_links_missing_and_pax(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'data'
            target.mkdir()
            relative = 'example.test/' + 'a' * 120 + '/go.mod'
            raw = tar_bytes([('go/pkg/mod/' + relative, b'module m')])
            self.assertEqual(seed.extract(io.BytesIO(raw), target, 'mod', 100,
                                          prefix='go/pkg/mod'), 8)
            self.assertEqual((target / relative).read_bytes(), b'module m')
            link = tarfile.TarInfo('go/pkg/mod/link')
            link.type, link.linkname = tarfile.SYMTYPE, '/etc/passwd'
            for entries in ([], [('go/pkg/mod/.wh.deleted', b'')], [('unrelated', b'x')],
                            [('../escape', b'x')], [link]):
                with self.subTest(entries=entries), self.assertRaises(ValueError):
                    seed.extract(io.BytesIO(tar_bytes(entries)), target, 'mod', 100, prefix='go/pkg/mod')
            empty = tarfile.TarInfo('go/pkg/mod')
            empty.type = tarfile.DIRTYPE
            self.assertEqual(seed.extract(io.BytesIO(tar_bytes([empty])), target, 'mod', 100,
                                          prefix='go/pkg/mod'), 0)

    def test_bootstrap_auth_and_integrity_metadata_before_large_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w:gz') as tar:
                member = tarfile.TarInfo('crane')
                member.size = 4
                tar.addfile(member, io.BytesIO(b'tool'))
            tool_bytes = raw.getvalue()
            manifest, config, raw_manifest, blobs, reference = image_fixture()
            calls = []
            instance = mock.Mock()
            instance.env = {'REGISTRY_AUTH_FILE': '/unowned/auth', 'XDG_CONFIG_HOME': '/unowned/podman'}
            instance.temporary.return_value = stage
            def command(args, **kwargs):
                calls.append((args, kwargs))
                self.assertEqual(json.loads((Path(instance.env['DOCKER_CONFIG']) / 'config.json').read_text()), {})
                if args[0] == 'curl':
                    data = tool_bytes
                elif args[1] == 'manifest':
                    data = raw_manifest
                else:
                    data = blobs[args[-1].split('@')[1]]
                if 'output' in kwargs:
                    kwargs['output'].write(data)
                else:
                    return data
            instance.command.side_effect = command
            with mock.patch.object(registry, 'SHA256', hashlib.sha256(tool_bytes).hexdigest()):
                image = registry.RegistryImage(instance, stage)
                image.acquire(reference)
            self.assertEqual(image.manifest(), {})
            self.assertEqual(len(calls), 4)  # tool, manifest, config, small metadata layer only
            self.assertEqual(calls[-1][1]['max_bytes'], manifest['layers'][-1]['size'])
            self.assertFalse(Path(instance.env['REGISTRY_AUTH_FILE']).exists())
            self.assertFalse((stage / 'layer.tar.gz').exists())
            # A digest failure never reaches the extractor or publication.
            blobs[manifest['layers'][1]['digest']] = b'corrupt'
            with self.assertRaisesRegex(ValueError, 'compressed layer'):
                image.blob('/root/.cache/go-build')

    def test_checksum_failure_prevents_executable_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            instance = mock.Mock()
            instance.env = {}
            instance.temporary.return_value = stage
            instance.command.side_effect = lambda args, **kwargs: kwargs['output'].write(b'bad download')
            image = registry.RegistryImage(instance, stage)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                image.acquire('matrixorigin/matrixone@sha256:' + '1' * 64)
            self.assertFalse(image.tool.exists())
            self.assertEqual(instance.command.call_count, 1)


if __name__ == '__main__':
    unittest.main()
