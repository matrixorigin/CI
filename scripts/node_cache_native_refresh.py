"""One-time native-only candidate; no UT, Go precompile or shared writes.

An independently reviewed immutable request pins a canonical main commit and
the exact native key observed on each consumer CPU. Ordinary PRs never call
this module. Candidate output is not publication authority.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import tarfile

import incremental_cache as c
import node_compile_cache as n
import publish_node_cache as p


def profile_scope(request,node):
    if request['schema']==1:
        c.require(request['environment']=='mty' and set(request['profiles'])=={'mo-standard-0','mo-standard-1'}
            and node in request['profiles'],'native refresh node scope differs')
    else:
        c.require(request['schema']==2 and request['environment'] in ('jed','mnl2','ruh')
            and node=='mo-standard-0' and set(request['profiles'])=={'mo-standard-0'}
            and request['target_nodes']==['mo-standard-0','mo-standard-1','mo-standard-2']
            and request['one_producer_per_uniform_environment_cpu'] is True
            and request['source_sha']=='87c44445f83e308ef12dd7ca0e570cec1e6690d9'
            and request['current_pr_head_sha']=='fe458bd2595923d616a6e43f33557f82ae860c8f'
            and request['native_input_count']==196,'unreviewed uniform environment native scope')
        expected=n.hash_value(request['profiles'][node]['runtime']['cpu'])
        c.require(request['target_cpu_sha256']=={v:expected for v in request['target_nodes']},
            'native producer does not cover the exact reviewed target CPUs')
    return request['environment']


def preflight(state,request,tools,catalog,source_sha,node):
    environment=profile_scope(request,node)
    c.require(request['source_repository']=='matrixorigin/matrixone'
        and request['source_sha']==source_sha and re.fullmatch('[0-9a-f]{40}',source_sha)
        and request['native_inputs_equal_current_pr'] is True
        and request['current_pr']==32871 and re.fullmatch('[0-9a-f]{40}',request['current_pr_head_sha'])
        and len(state['native_inputs'])==request['native_input_count'],
        'native refresh trusted source authority differs')
    expected=request['profiles'][node]
    if request['schema']==2:
        c.require(n.hash_value(state['native_inputs'])==request['native_inputs_sha256'],
            'uniform native source content differs from independent review')
    c.require(state['config']['runtime']==expected['runtime']
        and state['native_key']==expected['native_key']
        and c.HEX.fullmatch(state['native_key'])
        and state['entries'] and state['config']['indexes'],
        'native refresh differs from exact observed consumer key/runtime')
    c.require(state['config']['runtime']['environment']==environment
        and state['config']['runtime']['repository']=='matrixorigin/matrixone'
        and os.environ['CS_CACHE_NODE']==os.environ['CS_CACHE_EXPECTED_NODE']==node,
        'native refresh actual node/project differs')
    binding=n.verify_binding(catalog,Path(state['config']['shared']),environment,node,
        {k:os.environ['CS_CACHE_'+k.upper()] for k in ('node_uid','pvc_uid','pv_uid')})
    c.require(tools['binding']==binding and tools['persistent_tools'] is True
        and tools['external_tool_downloads']==0
        and tools['archive_sha256']==request['tool_archive_sha256']
        and tools['archive_bytes']==request['tool_archive_bytes'],'native refresh local tools/binding differ')
    c.require(n.native_manifest(state) is None,'native refresh already available; reconcile instead')
    return {'source_sha':source_sha,'node':node,'native_key':state['native_key'],
        'binding':binding,'compile_only':True,'native_only':True,'go_precompile':False,
        'ut_execution':False,'shared_cache_writes':0,'published':False}


def freeze(state_path,request,destination):
    state=c.read_json(state_path);source=c.safe_path(state['source']);cfg=state['config']
    local=c.safe_path(cfg['local']);destination=c.safe_path(destination)
    c.require(not destination.exists() and local not in destination.parents
        and destination not in local.parents,'native candidate must be fresh and disjoint')
    node=os.environ['CS_CACHE_NODE'];environment=profile_scope(request,node)
    revision=n.command(['git','rev-parse','HEAD'],source)
    c.require(revision==request['source_sha'] and cfg['runtime']==request['profiles'][node]['runtime']
        and n.runtime(source,environment,'matrixorigin/matrixone')==cfg['runtime'],
        'native refresh runtime/source moved')
    key,inputs=n.native_key(source,cfg['runtime'])
    c.require(key==state['native_key']==request['profiles'][node]['native_key']
        and inputs==state['native_inputs'],'native refresh inputs/build flags moved')
    report=c.read_json(state_path.parent/'native-result.json')
    c.require(report['native_hit'] is False and n.native_digest(source)==report['native_output_sha256'],
        'native candidate must be freshly built and unchanged')
    return pack_native(source,local,cfg,destination,key,inputs,revision,report)


def pack_native(source,local,cfg,destination,key,inputs,revision,report):
    """Pack validated private native outputs; neither selection nor publication authority."""
    destination.mkdir();native_key,mid=p.export_native(source,local,cfg['runtime'])
    c.require(native_key==key,'native key changed while freezing')
    manifest_path=local/'native'/(mid+'.json');native=c.read_json(manifest_path)
    c.require({'cgo/libmo.so','cgo/.mo-native-provenance'}<=set(native['outputs']),
        'native candidate lacks original Make provenance')
    # Empty action map by design: no ordinary Go cache is reproduced/exported.
    # Native generation identity keeps immutable indexes unique across keys.
    index={'schema':2,'runtime':cfg['runtime'],'actions':{},'native_generation':key,'source':revision}
    index_path=destination/'index.json';c.atomic_json(index_path,index);ident=c.digest(index_path)
    objects={}
    for item in native['outputs'].values():
        digest,size=item['sha256'],item['size']
        c.require(digest not in objects or objects[digest]==size,'native object size alias')
        objects[digest]=size
    files=[('index.json',index_path),('native/'+mid+'.json',manifest_path)]
    files += [('objects/'+digest[:2]+'/'+digest,local/'objects'/digest[:2]/digest) for digest in sorted(objects)]
    total=sum(path.stat().st_size for _,path in files)
    c.require(0<total<=8*c.GIB and shutil.disk_usage(destination).free>=3*total+20*c.GIB,
        'native candidate export budget')
    archive=destination/'delta.tar.gz'
    with tarfile.open(archive,'w:gz',compresslevel=1) as tar:
        for name,path in files:
            info=tarfile.TarInfo(name);info.size=path.stat().st_size;info.mode=0o444
            with c.safe_open(path) as body:tar.addfile(info,body)
    c.require(archive.stat().st_size<=8*c.GIB,'native compressed budget')
    value={'schema':2,'compile_only':True,'native_only':True,'go_precompile':False,
        'runtime':cfg['runtime'],'source':revision,'parent_indexes':cfg['indexes'],
        'index_sha256':ident,'native_manifests':{key:mid},
        'contract':{'kind':'native-only','native_input_count':len(inputs),'native_inputs_sha256':n.hash_value(inputs)},
        'object_count':len(objects),'object_bytes':sum(objects.values()),'unpacked_bytes':total,
        'archive_bytes':archive.stat().st_size,'archive_sha256':c.digest(archive),
        'compile_seconds':[report['native_seconds']],'shared_cache_writes':0,'ut_execution':False}
    c.atomic_json(destination/'manifest.json',value)
    return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=('preflight','freeze'))
    for name in ('state','request','catalog','destination'):parser.add_argument('--'+name,type=Path)
    args=parser.parse_args();state=c.read_json(args.state);request=c.read_json(args.request)
    if args.phase=='preflight':
        value=preflight(state,request,c.read_json(Path(os.environ['RUNNER_TEMP'])/'persistent-toolchain.json'),
            c.read_json(args.catalog),n.command(['git','rev-parse','HEAD'],Path(state['source'])),os.environ['CS_CACHE_NODE'])
        c.atomic_json(args.state.parent/'native-refresh-preflight.json',value)
    else:freeze(args.state,request,args.destination)


if __name__=='__main__':main()
