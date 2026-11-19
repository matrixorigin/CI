"""Small read-only PVC revalidation helpers under an explicit operator policy.

No cache/PVC mutations, compiler, downloads, pool edits or workflow dispatch.
Only fresh-gated create of own CM/Pods and exact-UID retirement of own Pods.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import time
import traceback
import incremental_cache as c
import node_cache_reader_control as control

q=control.q
SCANNER='d4009deaad719fbc39a71dbcaf6a4d9f4a6d3ce1';CONTRACT=44433331964
FILES=('incremental_cache.py','cs_coverage_cache.py','node_compile_cache.py','node_cache_toolchain.py',
    'node_cache_toolchain_store.py','node_cache_replication.py','node_cache_pvc_scan.py')


def run(policy,base,lock,evidence,registration,config,directory,scanner,api):
    env=policy['environment'];directory=Path(directory);scanner=Path(scanner)
    c.require(env in ('mty','jed','mnl2','ruh') and not directory.exists(),'review intent exists or environment unapproved')
    c.require(subprocess.check_output(['git','rev-parse','HEAD'],cwd=scanner,text=True).strip()==SCANNER
        and not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=scanner,text=True).strip(),
        'scanner source not immutable/clean')
    tested=api(f'repos/matrixorigin/CI/actions/runs/{CONTRACT}')
    c.require(tested['head_sha']==SCANNER and tested['conclusion']=='success' and tested['status']=='completed'
        and tested['path']=='.github/workflows/pr-node-cache-contract.yaml','scanner Linux contract missing')
    c.require(control.cache.hash_value(base)==evidence['catalog_canonical_sha256'],'source catalog changed')
    copy=next(v for v in evidence['copies'] if v['environment']==env)
    publication=api(f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{copy["run_id"]}')
    c.require(publication['head_sha']==copy['gitops_sha'] and publication['conclusion']=='success'
        and publication['status']=='completed' and publication['run_attempt']==1
        and publication['event']=='workflow_dispatch' and publication['path']=='.github/workflows/cloudsigma-up.yaml'
        and publication['actor']['login']==publication['triggering_actor']['login']=='Ariznawlll',
        'original protected publication provenance changed')
    source=next(v for v in base['nodes'] if (v['environment'],v['node'])==(env,'mo-standard-0'))
    for entry in control.cache.catalog_for_node(base,source)['entries']:
        p=entry['producer']
        control.cache.legacy.validate_provenance(entry,api(f'repos/{p["repository"]}/actions/runs/{p["id"]}'),
            api(f'repos/{p["repository"]}/actions/artifacts/{entry["artifact"]["id"]}'))
        c.require(entry['compile_only'] is True and p['repository']=='matrixorigin/matrixone'
            and p['event'] in ('push','workflow_dispatch'),'untrusted historical source')
    directory.mkdir(mode=0o700)
    state={'schema':1,'environment':env,'complete':False,'created':[],'deleted':[],
        'cluster_cache_writes':0,'compiler_invocations':0,'external_downloads':0,'cache_recreated':False,
        'automatic_retry':False,'revision':SCANNER,'contract_run':CONTRACT,'copy_authority':copy,
        'catalog_sha256':control.cache.hash_value(base),'targets':{},'started_at':q.pub.now().isoformat()}
    session=control.Session(policy,config,state,directory/'state.json');session.journal();baseline={}
    def guard():
        fresh=control.snapshots(session);proofs={}
        for node,snapshot in fresh.items():
            proofs[node]=q.admission(policy,snapshot,node,session.owned(),baseline.get(node),new_pods=1)
            if node not in baseline:baseline[node]=proofs[node]['workloads']
            session.cache_volume(next(v for v in base['nodes'] if (v['environment'],v['node'])==(env,node)))
        return proofs
    try:
        state['preflight']=guard()
        original=session.call(['get','autoscalingrunnerset',f'cs-{env}-large-7c14g','-n','cicd','-o','json'])
        state['original_pool']={'uid':original['metadata']['uid'],'spec_sha256':control.cache.hash_value(original['spec'])}
        c.require(state['original_pool']==registration['original_pool'],'original pool differs from accepted reader registration')
        name=f'cs-{env}-pvc-independent-review-r1'
        bindings=[v for v in base['nodes'] if v['environment']==env]
        c.require(len(bindings)==3,'review requires all existing node volumes')
        data={f:(scanner/'scripts'/f).read_text() for f in FILES}
        data.update({'catalog.json':c.canonical(base).decode(),'lock.json':c.canonical(lock).decode()})
        for b in bindings:data['binding-'+b['node'][-1]+'.json']=c.canonical(b).decode()
        c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'review ConfigMap too large')
        guard();session.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':'cicd'},'immutable':True,'data':data})
        state['instructions_sha256']=control.cache.hash_value(data);session.journal();templates={}
        for binding in bindings:
            node=binding['node'];podname=name+'-n'+node[-1];guard()
            pod=q.base.helper(podname,node,f'cs-{env}-incremental-n'+node[-1]+'-v1',name,True)
            pod['spec']['containers'][0]['env']=[{'name':'PYTHONDONTWRITEBYTECODE','value':'1'}]
            session.create(pod);templates[node]=pod;deadline=time.monotonic()+300
            while True:
                row=next(v for v in state['created'] if v['name']==podname)
                if q.checked_pod(pod,session.call(['get','pod',podname,'-n','cicd','-o','json']),row['uid']):break
                c.require(time.monotonic()<deadline,'readonly helper Pending; retain resources, no retry')
                time.sleep(2)
        guard()
        def verify(binding):
            node=binding['node'];podname=templates[node]['metadata']['name'];row=next(v for v in state['created'] if v['name']==podname)
            cm=session.call(['get','configmap',name,'-n','cicd','-o','json'])
            c.require(cm['metadata']['uid']==state['created'][0]['uid'] and cm['immutable'] is True
                and control.cache.hash_value(cm['data'])==state['instructions_sha256'],'readonly instructions changed')
            c.require(q.checked_pod(templates[node],session.call(['get','pod',podname,'-n','cicd','-o','json']),row['uid']),
                'helper no longer exact/Ready')
            report=session.call(['exec',podname,'-n','cicd','--','env','PYTHONDONTWRITEBYTECODE=1','python3',
                '/probe/node_cache_pvc_scan.py','--catalog','/probe/catalog.json','--lock','/probe/lock.json',
                '--binding','/probe/binding-'+node[-1]+'.json'],timeout=900)
            c.require(report['success'] is True and report['readonly'] is True and report['binding']==binding
                and report['catalog_sha256']==state['catalog_sha256'] and report['filesystem_writes']==0
                and report['compiler_invocations']==report['external_downloads']==0,'full byte verification failed')
            return node,report
        with ThreadPoolExecutor(max_workers=3) as executor:
            for node,report in executor.map(verify,bindings):state['targets'][node]=report;session.journal()
        state['postflight']=guard()
        after=session.call(['get','autoscalingrunnerset',f'cs-{env}-large-7c14g','-n','cicd','-o','json'])
        c.require(after['metadata']['uid']==state['original_pool']['uid'] and after['spec']==original['spec'],'original pool changed')
        for row in list(state['created']):
            if row['kind']=='Pod':guard();session.delete(row,'deleted')
        session.absent(state['deleted'])
        state.update(complete=True,helper_pods_absent=True,completed_at=q.pub.now().isoformat());session.journal()
        return state
    except Exception as error:
        state.update(error_type=type(error).__name__,failure_sites=[{'file':Path(v.filename).name,'line':v.lineno}
            for v in traceback.extract_tb(error.__traceback__)],failed_at=q.pub.now().isoformat())
        session.journal();raise
