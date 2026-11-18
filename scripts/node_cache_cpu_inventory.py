"""Read-only CPU/binding evidence; no compatibility alias or cache publication."""
import argparse
import json
from pathlib import Path

import incremental_cache as c
import node_compile_cache as cache
import node_cache_toolchain_store as tools
from node_cache_workspace_probe_job import cpu_profile


def checked_cpu(cpu):
    c.require(set(cpu)=={'vendor_id','cpu family','model','model name','stepping','flags'},'CPU inventory fields differ')
    c.require(all(isinstance(v,str) and 0<len(v)<16384 and '\n' not in v for v in cpu.values()),'CPU inventory values differ')
    c.require(all(cpu[k].isdigit() for k in ('cpu family','model','stepping')),'CPU numeric profile differs')
    return cache.hash_value(cpu)


def inventory(catalog,shared,environment,node,hints,cpu):
    digest=checked_cpu(cpu)
    binding=tools.binding(catalog,shared,environment,node,hints,True)
    reviewed=sorted({checked_cpu(e['runtime']['cpu']) for e in cache.catalog_for_node(catalog,binding)['entries']})
    c.require(reviewed,'no reviewed CPU profiles for node')
    return {'environment':environment,'node':node,'binding':binding,'cpu':cpu,'cpu_sha256':digest,
        'reviewed_cpu_sha256':reviewed,'exact_cpu_profile_present':digest in reviewed,
        'readonly':True,'filesystem_writes':0,'compiler_invocations':0,'external_tool_downloads':0,
        'cpu_profile_only':True,'full_runtime_compatibility_proven':False}


def plan(catalog,reports):
    """Group missing CPU profiles, not one complete cache rebuild per runner."""
    wanted={(b['environment'],b['node']):b for b in catalog['nodes']}
    c.require(len(reports)==len(wanted)==12,'all twelve CPU observations required')
    groups={};seen=set()
    for report in reports:
        key=report['environment'],report['node']
        c.require(key in wanted and key not in seen,'duplicate/unapproved CPU observation');seen.add(key)
        c.require(report['binding']==wanted[key] and report['cpu_sha256']==checked_cpu(report['cpu']),
            'CPU observation identity/hash differs')
        c.require(report['readonly'] and report['filesystem_writes']==report['compiler_invocations']==report['external_tool_downloads']==0,
            'CPU observation was not read-only')
        reviewed={checked_cpu(e['runtime']['cpu']) for e in cache.catalog_for_node(catalog,wanted[key])['entries']}
        c.require(set(report['reviewed_cpu_sha256'])==reviewed
            and report['exact_cpu_profile_present']==(report['cpu_sha256'] in reviewed),'CPU selection claim differs')
        if report['exact_cpu_profile_present']:continue
        group=key[0],report['cpu_sha256']
        value=groups.setdefault(group,{'environment':key[0],'cpu_sha256':report['cpu_sha256'],
            'cpu':report['cpu'],'nodes':[],'trusted_producer_required':True,'existing_generations_preserved':True})
        value['nodes'].append(key[1])
    return {'cpu_profile_only':True,'observed_nodes':12,'missing_cpu_profiles':list(groups.values()),
        'automatic_publication':False,'compatibility_aliases':False,'full_runtime_acceptance':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--binding-json',type=json.loads,required=True);a=parser.parse_args();b=a.binding_json
    result=inventory(c.read_json(a.catalog.resolve()),Path('/ci-cache'),b['environment'],b['node'],
        {k:b[k] for k in ('node_uid','pvc_uid','pv_uid')},cpu_profile())
    print(json.dumps(result))
