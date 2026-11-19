"""Read-only transfer planning from an independently approved native manifest."""
import stat
import incremental_cache as c
import node_compile_cache as cache


def plan(shared, entry, manifest):
    root=c.safe_path(shared)
    key=manifest['key']
    c.require(entry.get('native_only') is True and entry['native_manifests']=={key:cache.hash_value(manifest)}
              and entry['runtime']==manifest['runtime'] and manifest['compile_only'] is True,
              'unapproved native object inventory')
    wanted={}
    for name, record in manifest['outputs'].items():
        c.require(cache.legacy.native_allowed(name),'unexpected native output')
        ident=record['sha256'];size=record['size']
        c.check_record('0'*64,{'output_id':ident,'size':size})
        c.require(wanted.setdefault(ident,size)==size,'conflicting native object sizes')
    c.require(wanted,'empty native output inventory')
    missing={};present={}
    for ident,size in sorted(wanted.items()):
        path=c.safe_path(root/'objects'/ident[:2]/ident)
        try: st=path.lstat()
        except FileNotFoundError:
            missing[ident]=size
            continue
        c.require(stat.S_ISREG(st.st_mode) and st.st_size==size and c.digest(path)==ident,
                  'existing native object corrupt; preserve it and stop')
        present[ident]=size
    return {'missing':missing,'present':present,'payload_bytes':sum(missing.values()),
            'reused_bytes':sum(present.values()),'total_objects':len(wanted)}
