"""Tool-specific adapter to the same UID/resource/journal guarded controller."""
import copy
import datetime as dt
import json
import os
import urllib.error
import urllib.request

import incremental_cache as c
import node_cache_append_control as controller
import node_cache_publish_job as initial
import node_cache_toolchain_store as store

repository='matrixorigin/CI'
script='node_cache_toolchain_job.py'


def name(policy,entry):
    store.validate_lock(entry['toolchain'])
    return f"cs-{policy['environment']}-nc-tools-{entry['producer']['id']}-{entry['artifact']['id']}"


def objects(policy,entry,base,operation):
    c.require(operation==name(policy,entry),'tool operation differs')
    old=f"cs-{policy['environment']}-nc-append-{entry['producer']['id']}-{entry['artifact']['id']}"
    cm,pod=controller.objects(policy,entry,base,old)
    for resource in (cm,pod):
        resource['metadata']['name']=operation
        resource['metadata']['labels']={'codex.matrixorigin.io/experiment':operation}
    pod['spec']['volumes'][-1]['configMap']['name']=operation
    for filename in ('node_cache_toolchain.py','node_cache_toolchain_store.py',script):
        cm['data'][filename]=(controller.initial.HERE/filename).read_text()
    cm['data']['tool-catalog.json']=c.canonical(entry['catalog']).decode()
    c.require(sum(len(k.encode())+len(v.encode()) for k,v in cm['data'].items())<900000,'tool ConfigMap size')
    return [cm,pod]


def authorize(entry,request):
    stamp=dt.datetime.fromisoformat(request['checked_at'])
    c.require(stamp.tzinfo is not None and 0<=(dt.datetime.now(dt.timezone.utc)-stamp).total_seconds()<=120,
              'tool authorization stale')
    c.require(entry['producer']==entry['toolchain']['producer'] and
              entry['artifact']==entry['toolchain']['artifact'],'tool approval disagrees')
    store.authorize(entry['toolchain'],request['run'],request['artifact'])


def signed_url(entry):
    store.validate_lock(entry['toolchain'])
    req=urllib.request.Request('https://api.github.com/repos/'+repository+'/actions/artifacts/'+
        str(entry['artifact']['id'])+'/zip',headers={'Authorization':'Bearer '+os.environ['CS_CACHE_READ_TOKEN'],
        'Accept':'application/vnd.github+json'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    try:urllib.request.build_opener(NoRedirect()).open(req,timeout=30)
    except urllib.error.HTTPError as error:
        c.require(error.code==302,'tool artifact redirect unavailable')
        return initial.validate_url(error.headers['Location'])
    raise ValueError('tool artifact redirect missing')
