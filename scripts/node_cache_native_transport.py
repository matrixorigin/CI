"""Exact native-only two-member ZIP contract; same complete tar/hash verifier.

Legacy producers still require their original three-member ZIP. The native
producer uploads manifest+delta; index.json is authenticated inside the tar,
not duplicated at ZIP root. No archive/index/object hash is omitted.
"""
import io
from pathlib import Path
import urllib.request
import zipfile

import incremental_cache as c
import node_compile_cache as n
import node_cache_transport as legacy


def unpack(directory,reviewed):
    directory=c.safe_path(directory);transport=directory/'transport.zip';artifact=reviewed['artifact']
    c.require(reviewed.get('native_only') is True and len(reviewed.get('native_manifests',{}))==1,
        'two-member native format is not an ordinary candidate fallback')
    c.require(transport.stat().st_size==artifact['size_in_bytes'] and 'sha256:'+c.digest(transport)==artifact['digest'],
        'native ZIP transport differs')
    with c.safe_open(transport) as body,zipfile.ZipFile(body) as archive:
        members=archive.infolist()
        c.require(len(members)==2 and {m.filename for m in members}=={'manifest.json','delta.tar.gz'}
            and all(not m.is_dir() and not m.flag_bits&1 and (m.external_attr>>16)&0o170000 in (0,0o100000) for m in members),
            'native ZIP inventory/type differs')
        info=archive.getinfo('manifest.json');c.require(0<info.file_size<=c.MAX_INDEX,'native manifest budget')
        raw=archive.read(info)
        c.require(__import__('hashlib').sha256(raw).hexdigest()==reviewed['manifest_sha256'],'native manifest digest differs')
        c.atomic_file(directory/'manifest.json',io.BytesIO(raw),len(raw),reviewed['manifest_sha256'])
        value=c.read_json(directory/'manifest.json')
        c.require(value['schema']==2 and value['compile_only'] is True and value['native_only'] is True
            and value['go_precompile'] is False and value['ut_execution'] is False and value['shared_cache_writes']==0
            and value['runtime']==reviewed['runtime'] and value['source']==reviewed['source']
            and value['index_sha256']==reviewed['index_sha256']
            and value['native_manifests']==reviewed['native_manifests'] and value['contract']['kind']=='native-only',
            'native metadata differs from independent approval')
        info=archive.getinfo('delta.tar.gz')
        c.require(0<info.file_size==value['archive_bytes']<=8*c.GIB,'native compressed budget differs')
        with archive.open(info) as stream:
            c.atomic_file(directory/'delta.tar.gz',stream,info.file_size,value['archive_sha256'])
    return value


def receive(reviewed,directory,candidate,api=n.legacy.api,url_factory=legacy.signed_archive_url,
            open_url=urllib.request.urlopen):
    # The original full tar inventory/hash/size verifier authenticates and
    # derives candidate/index.json from the manifest-index SHA before use.
    index=legacy.receive(reviewed,directory,candidate,api,url_factory,open_url,unpack=unpack)
    value=c.read_json(index)
    c.require(value['actions']=={} and value['source']==reviewed['source']
        and value['native_generation']==next(iter(reviewed['native_manifests'])),
        'native candidate contains Go actions or another native generation')
    return index
