"""Offline raw-receipt acceptance for a separately authenticated CPU append.

Caller must first verify the protected run/artifact ZIP digest and immutable
controller revision. This validates content; it neither trusts a candidate by
itself nor publishes a catalog or changes Kubernetes resources.
"""
import copy
import datetime as dt
import re

import incremental_cache as c
import node_cache_profile_append as core
import node_cache_profile_append_control as controller


def verify(report, entry, base):
    core.check_approval(base, entry)
    binding=base['binding'];node=binding['node']
    c.require(binding['environment']=='mty' and node in controller.PROFILES and
        core.cpus.checked_cpu(entry['runtime']['cpu'])==controller.PROFILES[node],'CPU receipt scope differs')
    expected=f"cs-mty-nc-profile-n{node[-1]}-{entry['producer']['id']}-{entry['artifact']['id']}"
    c.require(report['name']==expected and report['environment']=='mty' and report['node']==node and
        report['complete'] is True and report['automatic_retry'] is False and report['compiler_invocations']==0 and
        report['existing_cache_pvc_preserved'] is True and report['catalog_activated'] is False and
        report['cpu_profile_sha256']==controller.PROFILES[node] and
        report['cpu_inventory_run']==controller.CPU_RUN and report['provenance_verified_before_write'] is True,
        'CPU receipt acceptance fields differ')
    c.require(not set(report)&{'pending_create','pending_delete','active_phase','error_type','failure_sites'},
        'CPU publication has ambiguous/failed operation')
    c.require(dt.datetime.fromisoformat(report['completed_at'])>dt.datetime.fromisoformat(report['started_at']),
        'CPU publication timing differs')
    made=report['created']
    c.require(len(made)==2 and [r['kind'] for r in made]==['ConfigMap','Pod'] and
        [r['name'] for r in made]==[expected,expected+'-writer'] and
        all(set(r)=={'kind','name','uid'} and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',r['uid']) for r in made)
        and len({r['uid'] for r in made})==2 and report['deleted']==[made[1]] and
        report['helper_pods_absent'] is True and report['private_stage_collected'] is True,
        'CPU publisher resource/UID cleanup differs')
    work=report['workspace']
    c.require(work['pod_name']==made[1]['name'] and work['pod_uid']==made[1]['uid'] and
        work['pvc_name']==work['pod_name']+'-work' and work['bytes']==80*c.GIB and
        all(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',work[k]) for k in ('pvc_uid','pv_uid')) and
        work['pvc_uid']!=binding['pvc_uid'] and work['pv_uid']!=binding['pv_uid'] and
        report['workspace_gc']==[work] and report['workspaces']==[work],
        'CPU stage was not a separate owner-GC private volume')
    for phase in ('stage','append','verify'):
        value=report[phase]
        c.require(value['phase']==phase and value['success'] is True and value['compiler_invocations']==0 and
            value['index_sha256']==entry['index_sha256'] and value['controller_seconds']>=0,
            'CPU publication phase differs')
        if phase=='stage':c.require(value['shared_cache_modified'] is False,'CPU staging changed shared cache')
        else:
            c.require(value['binding']==binding and value['marker_sha256']==base['marker_sha256'] and
                value['old_metadata_preserved'] is True and value['independently_verified'] is True and
                value['compatibility_aliases'] is False,'CPU publication removed/aliased old metadata')
    added=report['append']
    c.require(added['cpu_profile_sha256']==controller.PROFILES[node] and
        added['new_bytes']>=0 and added['existing_bytes']>=0,'CPU content-addressed publication counters differ')
    c.require(set(report['preflight'])==set(report['postflight'])=={'mo-standard-0','mo-standard-1','mo-standard-2'},
        'CPU all-node resource proof incomplete')
    for target,before in report['preflight'].items():
        after=report['postflight'][target]
        c.require(before['workloads']==after['workloads'] and before['root_free_bytes']>=60*c.GIB and
            after['root_free_bytes']>=20*c.GIB and before['storage_class_uid']==after['storage_class_uid'],
            'CPU original workloads/resources changed')
    c.require(report['latest_resources']['root_free_bytes']>=20*c.GIB,'CPU runtime root reserve breached')
    # An output proposal is not catalog activation. Old copy evidence remains
    # independent authority; the new entry is bound ONLY to this verified node.
    proposed=copy.deepcopy(entry)
    proposed['bindings']=[{k:binding[k] for k in ('node_uid','pvc_uid','pv_uid')}]
    return {'binding':copy.deepcopy(binding),'marker_sha256':base['marker_sha256'],
        'preserved_indexes':[e['index_sha256'] for e in base['existing_entries']],
        'additional_entry':proposed,'catalog_activated':False,'full_pr_ut_acceptance':False}
