"""Separately reviewed native-only publisher, exact existing volumes only.

Candidate metadata never approves itself. The protected wrapper pins its entire
entry/base pair, and this policy pins the source, producer helper/caller and CPU
request. Existing parents retain their independent persistent authority.
"""
from pathlib import Path
import incremental_cache as c
import node_compile_cache as cache
import node_cache_pvc_authority as pvc
import node_cache_pr_native_catalog as native
import node_cache_native_refresh as producer
import node_cache_reader_rollout as readers
import node_cache_profile_append_control as engine
import node_cache_profile_receipt as receipt
import node_cache_publish_job as authorization

PRODUCER_CALLER='e7ccc11b77990099d896cbe75bb34478404e5531'
PRODUCER_CI='586b3c46ac63a3bcae05902679fad64d64ed8ecb'
REQUESTS={'jed':'3bbdd1badc8f54acf3dbb8b025beb9220c4215fabc7392796297ccbc0a1da20c',
    'mnl2':'d63345e3ba444e31b4d7eba0723ff34f4bcfd387d901840a0b84f3a7a5889342',
    'ruh':'11e5069c8816d65f2c8311eff59cf7744e78817e16d18193a32923e09e7987d5'}


class UniformNativePolicy:
    script='node_cache_persistent_native_job.py'

    def __init__(self,request,config):
        self.environment=producer.profile_scope(request,'mo-standard-0')
        c.require(cache.hash_value(request)==REQUESTS[self.environment],'uniform native independent request changed')
        self.request=request;self.config=Path(config)
        source,_=native.load(self.config);self.catalog=pvc.load_family(self.config,source)
        self.original=c.read_json(self.config/'node-compile-cache-catalog.json')
        self.cpu_review=c.read_json(self.config/'node-cache-reviewed-cpu-plan.json')
        self.profiles=request['target_cpu_sha256']
        self.cpu_inventory_run=readers.CPU_AUTHORITIES[self.environment][0]

    def scope(self,policy,catalog,entry,base):
        engine.core.check_approval(base,entry);node=base['binding']['node'];env=self.environment
        c.require(catalog==self.catalog and policy['environment']==base['binding']['environment']==env
            and node in self.profiles,'uniform native publication environment/node differs')
        profile=self.request['profiles']['mo-standard-0'];p=entry['producer']
        c.require(entry['native_only'] is True and entry['compile_only'] is True
            and entry['source']==self.request['source_sha'] and entry['runtime']==profile['runtime']
            and set(entry['native_manifests'])=={profile['native_key']},'uniform native source/key/runtime differs')
        c.require(p['repository']=='matrixorigin/matrixone' and p['head_sha']==PRODUCER_CALLER
            and p['head_branch']=='weilu-runner-test' and p['event']=='workflow_dispatch'
            and p['actor']=='Ariznawlll' and p['run_attempt']==1
            and p['path']=='.github/workflows/race-seed-canary.yaml'
            and any(w['sha']==PRODUCER_CI and w['path']=='matrixorigin/CI/.github/workflows/node-cache-uniform-native-refresh.yaml@'+PRODUCER_CI
                for w in p['referenced_workflows']),'uniform native successful producer identity differs')
        binding=next(b for b in catalog['nodes'] if (b['environment'],b['node'])==(env,node))
        c.require(base['binding']=={k:binding[k] for k in base['binding']} and base['marker_sha256']==binding['marker_sha256']
            and policy['node_uids'][node]==binding['node_uid']
            and cache.catalog_for_node(catalog,binding)['entries']==base['existing_entries'],
            'uniform native volume changed or parent omitted')
        name=f"cs-{env}-nc-profile-n{node[-1]}-{p['id']}-{entry['artifact']['id']}"
        c.require(len(name)<=63 and type(p['id']) is int and type(entry['artifact']['id']) is int,
            'uniform native operation name invalid')
        return binding,name

    def authenticate(self,catalog,lock,evidence,api):
        c.require(catalog==self.catalog and lock==c.read_json(self.config/'node-toolchain-lock.json')
            and evidence==c.read_json(self.config/'node-cache-all-node-copy-evidence.json'),
            'uniform native original catalog/tools/copy authority differs')
        for binding in catalog['nodes']:
            if binding['environment']!=self.environment:continue
            local=cache.catalog_for_node(catalog,binding)
            authority=pvc.Authority(local,api,lambda:api(pvc.REVOCATION_PATH))
            c.require(all(authority.covers(e) and authority.permits(e,e['runtime']) is True for e in local['entries']),
                'uniform native persistent base not authorized')

    def cpu_authority(self,api):
        cpus=readers.cpu_inventory(self.cpu_review,self.original,self.environment,api)
        c.require({node:cache.hash_value(cpu) for node,cpu in cpus.items()}==self.profiles,
            'uniform native all-node CPU proof differs')

    def fresh(self,entry,base,api):
        stamp=engine.initial.now().isoformat();p=entry['producer']
        result={'checked_at':stamp,'binding':base['binding'],
            'run':api(f'repos/{p["repository"]}/actions/runs/{p["id"]}'),
            'artifact':api(f'repos/{p["repository"]}/actions/artifacts/{entry["artifact"]["id"]}')}
        local=cache.catalog_for_node(self.catalog,next(b for b in self.catalog['nodes'] if
            {k:b[k] for k in base['binding']}==base['binding']))
        ident=local['persistent_pvc_authority']['authorization_id']
        issuer=pvc.APPROVAL_ISSUER_CONTRACT if ident==pvc.APPROVAL_SHA256 else pvc.FAMILY_ISSUER_CONTRACT
        paths={f'repos/matrixorigin/CI/actions/runs/{pvc.SCANNER_CONTRACT}',f'repos/matrixorigin/CI/actions/runs/{issuer}'}
        paths.update(f'repos/{e["producer"]["repository"]}/actions/runs/{e["producer"]["id"]}' for e in base['existing_entries'])
        result['persistent_primary_metadata']={path:api(path) for path in sorted(paths)}
        result['live_revocations_git_file']=api(pvc.REVOCATION_PATH)
        # Candidate expiry/provenance remains mandatory; old parent expiry does not.
        authorization.authorize(entry,result)
        return result

    def instructions(self,entry,base):
        data=engine.instructions(entry,base)
        for name in (self.script,'node_cache_persistent_native_append.py','node_cache_native_missing_plan.py','node_cache_native_selective.py','node_cache_pvc_authority.py'):
            data[name]=(Path(__file__).parent/name).read_text()
        data['persistent-catalog.json']=c.canonical(self.catalog).decode()
        c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'uniform native instructions exceed budget')
        return data

    def selective_stage(self,entry,base,api,invoke):
        import urllib.request
        import node_cache_native_selective as selective
        self.fresh(entry,base,api)
        url=authorization.validate_url(engine.transport.signed_archive_url(entry))
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        size=entry['artifact']['size_in_bytes']
        c.require(0<size<=selective.LIMIT,'source ZIP budget exceeded')
        with urllib.request.build_opener(NoRedirect).open(url,timeout=90) as stream:
            c.require(stream.status==200 and int(stream.headers['Content-Length'])==size,'source ZIP length changed')
            raw=stream.read(size+1)
        return selective.transfer(entry,raw,invoke,lambda:self.fresh(entry,base,api))

    def verify_receipt(self,report,entry,base,policy,catalog):
        self.scope(policy,catalog,entry,base)
        c.require(report['native_only'] is True and report['source']==entry['source']
            and report['native_key']==next(iter(entry['native_manifests'])),'uniform native receipt differs')
        stage=report['stage']
        c.require(stage['missing_object_bytes']==stage['acknowledged_payload_bytes']==report['append']['new_bytes']
            and stage['node_reused_bytes']==report['append']['existing_bytes']
            and stage['base_objects_transmitted']==0 and stage['source_candidate_zip_bytes']==entry['artifact']['size_in_bytes'],
            'target transfer and physical append counters disagree')
        return receipt.verify(report,entry,base,profiles=self.profiles,environment=self.environment,
            cpu_inventory_run=self.cpu_inventory_run)
