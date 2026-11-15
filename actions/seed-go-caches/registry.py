"""Narrow, digest-pinned COPY-layer transport for the trusted ci-builder format.

Not a general OCI rootfs merger: the trusted producer contract isolates the
selected caches from omitted layers. History checks detect layout drift; they
are not proof of arbitrary layer contents. No image code is executed.
"""
import gzip
import hashlib
import json
from pathlib import PurePosixPath
import re
import shutil
import tarfile

VERSION = '0.22.1'
SHA256 = '0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0'
URL = f'https://github.com/google/go-containerregistry/releases/download/v{VERSION}/go-containerregistry_Linux_x86_64.tar.gz'
PAYLOAD_LIMIT = 24 * 1024**3
BLOB_LIMIT = 8 * 1024**3
TOOL_LIMIT = 32 * 1024**2
METADATA_LIMIT = 65536
COPY_PATHS = (
    ('/home/runner/go/pkg/mod', '/go/pkg/mod'),
    ('/root/.cache/go-build', '/root/.cache/go-build'),
    ('/home/runner/_work/matrixone/matrixone/thirdparties/install', '/mo-prebuilt/thirdparties/install'),
    ('/mo-prebuilt/thirdparties.fingerprint', '/mo-prebuilt/thirdparties.fingerprint'),
    ('/mo-prebuilt/warm-status', '/mo-prebuilt/warm-status'),
    ('/mo-prebuilt/go-cache-manifest.json', '/mo-prebuilt/go-cache-manifest.json'),
)


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def descriptor(value, limit):
    if (not isinstance(value, dict)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(value.get('digest', '')))
            or type(value.get('size')) is not int or not 0 < value['size'] <= limit):
        raise ValueError('invalid or oversized registry descriptor')
    return value


def layout(manifest, config):
    layers = manifest.get('layers', [])
    history = [h.get('created_by') for h in config.get('history', []) if not h.get('empty_layer')]
    diff_ids = config.get('rootfs', {}).get('diff_ids', [])
    expected = [f'COPY {src} {dst} # buildkit' for src, dst in COPY_PATHS]
    if (config.get('os') != 'linux' or config.get('architecture') != 'amd64'
            or len(layers) < 6 or len(layers) != len(history) or len(layers) != len(diff_ids)
            or history[-6:] != expected):
        raise ValueError('unsupported ci-builder COPY-layer layout')
    selected = {}
    for (_, path), layer, diff_id in zip(COPY_PATHS, layers[-6:], diff_ids[-6:]):
        if layer.get('mediaType') not in ('application/vnd.oci.image.layer.v1.tar+gzip',
                                         'application/vnd.docker.image.rootfs.diff.tar.gzip'):
            raise ValueError('unsupported cache layer encoding')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(diff_id)):
            raise ValueError('invalid uncompressed layer digest')
        selected[path] = (descriptor(layer, BLOB_LIMIT), diff_id)
    descriptor(selected[COPY_PATHS[-1][1]][0], METADATA_LIMIT)
    return selected


class DigestReader:
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit
        self.hash = hashlib.sha256()
        self.size = 0

    def read(self, size):
        data = self.stream.read(min(size, 1024 * 1024))
        self.size += len(data)
        if self.size > self.limit:
            raise ValueError('uncompressed cache layers exceed budget')
        self.hash.update(data)
        return data

    def finish(self, expected):
        # tar stops at its end marker, but the gzip/diff_id contract covers the
        # entire uncompressed stream, including padding and concatenated members.
        while self.read(1024 * 1024):
            pass
        if 'sha256:' + self.hash.hexdigest() != expected:
            raise ValueError('uncompressed layer digest mismatch')


class RegistryImage:
    def __init__(self, seeder, cache):
        self.seeder = seeder
        self.stage = seeder.temporary(cache)
        self.tool = self.stage / 'crane'
        self.remaining = PAYLOAD_LIMIT

    def command(self, *args, **kwargs):
        return self.seeder.command([str(self.tool), *args], **kwargs)

    def acquire(self, image):
        seeder = self.seeder
        config = self.stage / 'auth'
        config.mkdir()
        (config / 'config.json').write_text('{}\n')
        seeder.env.update(DOCKER_CONFIG=str(config), XDG_CONFIG_HOME=str(config),
                          XDG_RUNTIME_DIR=str(config), REGISTRY_AUTH_FILE=str(config / 'absent'))
        tool_archive = self.stage / 'tool.tar.gz'
        with tool_archive.open('wb') as out:
            seeder.command(['curl', '--disable', '--fail', '--silent', '--show-error', '--location',
                            '--proto', '=https', '--proto-redir', '=https',
                            '--connect-timeout', '10', '--max-time', '120', URL],
                           output=out, timeout=125, max_bytes=TOOL_LIMIT)
        if hashlib.sha256(tool_archive.read_bytes()).hexdigest() != SHA256:
            raise ValueError('crane release checksum mismatch')
        with tarfile.open(tool_archive, 'r:gz') as archive:
            member = archive.getmember('crane')
            if not member.isfile() or member.size > 64 * 1024**2:
                raise ValueError('invalid crane executable')
            with archive.extractfile(member) as src, self.tool.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        self.tool.chmod(0o700)
        tool_archive.unlink()
        self.repository, expected = image.split('@')
        raw = self.command('manifest', image, max_bytes=METADATA_LIMIT)
        if digest(raw) != expected:
            raise ValueError('image manifest digest mismatch')
        manifest = json.loads(raw)
        config_desc = descriptor(manifest.get('config'), METADATA_LIMIT)
        raw = self.command('blob', self.repository + '@' + config_desc['digest'],
                           max_bytes=config_desc['size'])
        if len(raw) != config_desc['size'] or digest(raw) != config_desc['digest']:
            raise ValueError('image config digest mismatch')
        self.layers = layout(manifest, json.loads(raw))
        return dict(Id=image, Os='linux', Architecture='amd64', Size=PAYLOAD_LIMIT)

    def blob(self, source):
        desc, diff_id = self.layers[source]
        path = self.stage / 'layer.tar.gz'
        with path.open('xb') as out:
            self.command('blob', self.repository + '@' + desc['digest'], output=out,
                         timeout=900, max_bytes=desc['size'])
        hashed = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hashed.update(block)
        if path.stat().st_size != desc['size'] or 'sha256:' + hashed.hexdigest() != desc['digest']:
            raise ValueError('compressed layer digest or size mismatch')
        return path, diff_id

    def manifest(self):
        path, expected = self.blob(COPY_PATHS[-1][1])
        metadata = None
        seen = False
        with gzip.open(path, 'rb') as compressed:
            reader = DigestReader(compressed, METADATA_LIMIT)
            with tarfile.open(fileobj=reader, mode='r|') as archive:
                for member in archive:
                    archive.members.clear()
                    name = PurePosixPath(member.name)
                    if member.isdir() and name.as_posix() == 'mo-prebuilt':
                        continue
                    if (name.as_posix() != 'mo-prebuilt/go-cache-manifest.json' or not member.isfile()
                            or member.size > 8192 or seen):
                        raise ValueError('invalid producer metadata layer')
                    seen = True
                    metadata = json.load(archive.extractfile(member))
            reader.finish(expected)
        path.unlink()
        if metadata is None:
            raise ValueError('missing producer metadata')
        return metadata

    def extract(self, source, destination, root, budget, build, extractor):
        path, expected = self.blob(source)
        with gzip.open(path, 'rb') as compressed:
            reader = DigestReader(compressed, self.remaining)
            size = extractor(reader, destination, root, budget, build, prefix=source.lstrip('/'))
            reader.finish(expected)
            self.remaining -= reader.size
        path.unlink()
        return size
