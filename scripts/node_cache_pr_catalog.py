"""Review immutable PR-reader catalog without renewing the copied base.

This is structural evidence validation, not cluster/publication permission.
Consumers still authenticate producer APIs and their original expiry.
"""
import hashlib
import json
from pathlib import Path
import sys

import incremental_cache as c
import node_compile_cache as cache
import node_cache_profile_receipt as receipt


def validate(base,copied,active,proof):
    c.require(proof['schema']==1 and cache.hash_value(base)==proof['base_catalog_sha256']==copied['catalog_canonical_sha256'],
        'PR catalog changed original copied authority')
    approval=proof['append_approval'];run=approval['controller_run'];artifact=approval['artifact']
    c.require(run=={'id':44340319013,'head_sha':'a55b220c33420e00472556119c4c0815adbef4c7','workflow_id':289741451}
        and artifact['name']=='node-cache-profile-append-mty-44340319013'
        and artifact['digest']==proof['transport_digest'] and type(artifact['id']) is int and artifact['id']>0
        and c.HEX.fullmatch(artifact['digest'].removeprefix('sha256:')),
        'PR catalog protected append authority differs')
    raw=proof['raw_receipt'].encode()
    c.require(len(raw)<=2*1024**2 and hashlib.sha256(raw).hexdigest()==approval['report_sha256'],
        'PR catalog approved raw receipt changed')
    def unique(pairs):
        out={}
        for k,v in pairs:c.require(k not in out,'duplicate raw receipt JSON key');out[k]=v
        return out
    report=json.loads(raw,object_pairs_hook=unique)
    c.require(report['recovery_of']['run_id']==44338220751 and report['recovery_of']['empty_private_stage_verified'] is True
        and report['recovery_of']['new_pods']==report['recovery_of']['new_pvcs']==0
        and report['adopted_resources']==report['created'],'PR catalog adopted publisher identity differs')
    entry=approval['entry'];append_base=approval['base'];binding=append_base['binding']
    c.require(binding['environment']=='mty' and binding['node']=='mo-standard-1'
        and entry['source']=='dbc7ca14ca8b01cfe6d9e9a151619ea4b5ba14ca'
        and entry['producer']['id']==44326799783 and entry['artifact']['id']==14440043306,
        'PR catalog CPU generation/source differs')
    actual=next(b for b in base['nodes'] if (b['environment'],b['node'])==('mty','mo-standard-1'))
    c.require({k:actual[k] for k in binding}==binding and actual['marker_sha256']==append_base['marker_sha256']
        and cache.catalog_for_node(base,binding)['entries']==append_base['existing_entries'],
        'PR catalog append did not preserve original volume/base generations')
    accepted=receipt.verify(report,entry,append_base)
    c.require(active=={**base,'entries':base['entries']+[accepted['additional_entry']]},
        'PR catalog modified existing data/expiry/bindings or added unreviewed generations')
    return {'protected_append_run':run['id'],'node':'mo-standard-1','old_copy_authority_preserved':True,
        'other_environments_unchanged':True,'live_pr_ut_accepted':False}


if __name__=='__main__':
    c.require(sys.argv[1:]==['check'],'unknown PR catalog operation')
    root=Path(__file__).resolve().parents[1]/'config'
    values=[c.read_json(root/name) for name in ('node-compile-cache-catalog.json',
        'node-cache-all-node-copy-evidence.json','node-compile-cache-pr-catalog.json','node-cache-pr-profile-proof.json')]
    print(c.canonical(validate(*values)).decode())
