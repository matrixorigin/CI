"""Protected native-only append policy. Old CPU-repair scope stays unchanged.

No producer/source execution or compiler in the publisher. Approval files are
independent fixed inputs, not artifacts asserting their own publication trust.
"""
import argparse
from pathlib import Path

import incremental_cache as c
import node_compile_cache as cache
import node_cache_pr_catalog as catalog_review
import node_cache_profile_append_control as engine
import node_cache_profile_receipt as receipt

PRODUCER_CALLER='fcb1d43f1c91461f6a3182d6c9a3a00e4b21fe64'
PRODUCER_CI='53e8f5d10e6f1389101afb86c67b0ecdea131bd3'
SOURCE='87c44445f83e308ef12dd7ca0e570cec1e6690d9'


class NativeOnlyPolicy:
    def __init__(self,request,original,proof):
        c.require(request['schema']==1 and request['source_repository']=='matrixorigin/matrixone'
            and request['source_sha']==SOURCE and request['current_pr']==32871
            and request['current_pr_head_sha']=='706e5fa741aa6905877dab3e42ae73768b118e2b'
            and request['native_input_count']==196 and request['native_inputs_equal_current_pr'] is True
            and set(request['profiles'])=={'mo-standard-0','mo-standard-1'},'native append immutable request differs')
        self.request=request;self.original=original;self.proof=proof
        self.profiles={n:engine.core.cpus.checked_cpu(v['runtime']['cpu']) for n,v in request['profiles'].items()}

    def scope(self,policy,catalog,entry,base):
        engine.core.check_approval(base,entry)
        node=base['binding']['node'];c.require(policy['environment']=='mty' and node in self.profiles,'native append node scope differs')
        expected=self.request['profiles'][node];producer=entry['producer']
        c.require(entry.get('native_only') is True and entry['compile_only'] is True
            and entry['source']==SOURCE and entry['runtime']==expected['runtime']
            and set(entry['native_manifests'])=={expected['native_key']},'native append candidate exact input/runtime differs')
        c.require(producer['repository']=='matrixorigin/matrixone' and producer['head_sha']==PRODUCER_CALLER
            and producer['head_branch']=='weilu-runner-test' and producer['event']=='workflow_dispatch'
            and producer['actor']=='Ariznawlll' and producer['run_attempt']==1
            and producer['path']=='.github/workflows/race-seed-canary.yaml'
            and any(row['sha']==PRODUCER_CI and row['path']==
                'matrixorigin/CI/.github/workflows/node-cache-native-refresh.yaml@'+PRODUCER_CI
                for row in producer['referenced_workflows']),'native append reviewed producer authority differs')
        rows=[b for b in catalog['nodes'] if (b['environment'],b['node'])==('mty',node)]
        c.require(len(rows)==1,'native append volume missing');binding=rows[0]
        c.require(base['binding']=={k:binding[k] for k in base['binding']}
            and base['marker_sha256']==binding['marker_sha256'] and policy['node_uids'][node]==binding['node_uid']
            and cache.catalog_for_node(catalog,binding)['entries']==base['existing_entries'],
            'native append changed volume identity or omitted existing published generation')
        name=f"cs-mty-nc-profile-n{node[-1]}-{producer['id']}-{entry['artifact']['id']}"
        c.require(len(name)<=63 and all(type(entry[k]['id']) is int and entry[k]['id']>0 for k in ('producer','artifact')),
            'native append operation name invalid')
        return binding,name

    def authenticate(self,catalog,lock,evidence,api):
        catalog_review.validate(self.original,evidence,catalog,self.proof)
        engine.q.authenticate(self.original,lock,evidence,'mty',api)
        # Raw receipt + transport digest are already independently reviewed in
        # this immutable helper. Recheck the protected API identities, without
        # downloading/re-auditing the accepted 2 GB prior bundle again.
        approved=self.proof['append_approval'];expected=approved['controller_run']
        run=api(f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{expected["id"]}')
        c.require(run['id']==expected['id'] and run['head_sha']==expected['head_sha']
            and run['workflow_id']==expected['workflow_id'] and run['run_attempt']==1
            and run['status']=='completed' and run['conclusion']=='success'
            and run['event']=='workflow_dispatch' and run['head_branch']=='codex/ruh-pvc-readiness'
            and run['actor']['login']==run['triggering_actor']['login']=='Ariznawlll',
            'accepted CPU append protected run changed')
        artifact=api(f'repos/matrixorigin/cloudsigma-gitops/actions/artifacts/{approved["artifact"]["id"]}')
        c.require(artifact.get('expired') is False
            and all(artifact[k]==v for k,v in approved['artifact'].items())
            and artifact['workflow_run']['id']==run['id'] and artifact['workflow_run']['head_sha']==run['head_sha'],
            'accepted CPU append raw artifact API changed')

    def verify_receipt(self,report,entry,base,policy,catalog):
        self.scope(policy,catalog,entry,base)
        c.require(report['native_only'] is True and report['source']==SOURCE
            and report['native_key']==next(iter(entry['native_manifests'])),'native publication receipt scope differs')
        return receipt.verify(report,entry,base,profiles=self.profiles)


def main():
    parser=argparse.ArgumentParser()
    for name in ('policy','catalog','original','proof','request','lock','evidence','entry','base','kubeconfig','directory'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    operation=NativeOnlyPolicy(c.read_json(args.request),c.read_json(args.original),c.read_json(args.proof))
    engine.run(c.read_json(args.policy),c.read_json(args.catalog),c.read_json(args.lock),c.read_json(args.evidence),
        c.read_json(args.entry),c.read_json(args.base),args.kubeconfig,args.directory,operation=operation)


if __name__=='__main__':main()
