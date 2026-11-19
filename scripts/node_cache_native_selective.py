"""Native-only data plane. Controller selects; target never receives the source ZIP."""
import base64
import hashlib
import io
import json
import tarfile
import zipfile
import zlib

import incremental_cache as c
import node_cache_native_missing_plan as inventory

CHUNK=1024**2
LIMIT=128*1024**2


def sha(raw):return hashlib.sha256(raw).hexdigest()


def approved_source(raw,entry):
    a=entry['artifact']
    c.require(entry.get('native_only') is True and len(raw)==a['size_in_bytes']<=LIMIT
              and 'sha256:'+sha(raw)==a['digest'],'source ZIP differs from approved transport')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        c.require(sorted(z.namelist())==['delta.tar.gz','manifest.json'] and z.getinfo('manifest.json').file_size<=c.MAX_INDEX
                  and z.getinfo('delta.tar.gz').file_size<=LIMIT,'unexpected native transport')
        manifest=z.read('manifest.json'); archive=z.read('delta.tar.gz')
    c.require(sha(manifest)==entry['manifest_sha256'],'unapproved source manifest')
    m=json.loads(manifest)
    c.require(m['native_only'] is m['compile_only'] is True and m['go_precompile'] is m['ut_execution'] is False
              and m['source']==entry['source'] and m['runtime']==entry['runtime'] and m['shared_cache_writes']==0
              and (len(archive),sha(archive))==(m['archive_bytes'],m['archive_sha256'])
              and m['native_manifests']==entry['native_manifests'] and m['index_sha256']==entry['index_sha256']
              and m['unpacked_bytes']<=LIMIT,'native source contract differs')
    key,mid=next(iter(entry['native_manifests'].items()))
    meta={'index.json':entry['index_sha256'],'native/'+mid+'.json':mid}
    objects={};seen=set();size=0;docs={}
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r|gz') as t:
        for member in t:
            path=member.name
            c.require(member.isfile() and path not in seen and 0<=member.size<=LIMIT-size
                      and len(seen)<10000,'invalid native source member')
            seen.add(path);size+=member.size
            data=t.extractfile(member).read();ident=sha(data)
            c.require(len(data)==member.size,'short native source member')
            if path in meta:
                c.require(meta[path]==ident and len(data)<=c.MAX_INDEX,'source metadata differs')
                docs[path]=json.loads(data)
            else:
                c.require(path=='objects/'+ident[:2]+'/'+ident,'source object path/hash differs')
                objects[ident]=data
    native=docs['native/'+mid+'.json'];index=docs['index.json']
    c.require(size==m['unpacked_bytes'] and len(objects)==m['object_count'] and sum(map(len,objects.values()))==m['object_bytes']
              and {r['sha256']:r['size'] for r in native['outputs'].values()}=={i:len(b) for i,b in objects.items()}
              and index=={'schema':2,'actions':{},'runtime':entry['runtime'],'native_generation':key,'source':entry['source']}
              and inventory.cache.hash_value(index)==entry['index_sha256'] and inventory.cache.hash_value(native)==mid,
              'source object set/metadata incomplete')
    return index,native,objects


def chunks(data):
    return [data[n:n+CHUNK] for n in range(0,len(data),CHUNK)]


def transfer(entry,raw,invoke,fresh):
    index,native,objects=approved_source(raw,entry)
    parts={i:[sha(b) for b in chunks(data)] for i,data in objects.items()}
    want=invoke('inventory',{**fresh(),'index':index,'native':native,'parts':parts})
    needed=want['needed'];retained=want['retained']
    combined=needed+retained
    c.require(len(combined)==len({(i,pos) for i,pos in combined}) and all(i in parts and type(pos) is int
              and 0<=pos<len(parts[i]) for i,pos in combined),'duplicate/unknown object request')
    prior=sum(min(CHUNK,len(objects[i])-pos*CHUNK) for i,pos in retained)
    total=prior;encoded=0
    for ident,pos in needed:
        c.require(ident in parts and type(pos) is int and 0<=pos<len(parts[ident]),'unknown object request')
        data=objects[ident][pos*CHUNK:(pos+1)*CHUNK]
        wire=base64.b64encode(zlib.compress(data,1)).decode('ascii')
        ack=invoke('chunk',{'object':ident,'part':pos,'wire':wire})
        c.require(ack=={'object':ident,'part':pos,'payload':len(data),'encoded_bytes':len(wire)},'missing target acknowledgment')
        total+=len(data);encoded+=len(wire)
    result=invoke('stage',fresh())
    c.require(result['missing_object_bytes']==total and result['node_reused_bytes']+total==sum(map(len,objects.values())),
              'payload accounting does not match approved inventory')
    return {**result,'source_candidate_zip_bytes':len(raw),'acknowledged_payload_bytes':total,
            'retained_chunk_bytes':prior,'this_attempt_payload_bytes':total-prior,'this_attempt_encoded_bytes':encoded}


def begin(shared,stage,entry,index,native,parts):
    stage=c.safe_path(stage); planned=inventory.plan(shared,entry,native)
    objects=planned['missing']|planned['present']
    c.require(set(parts)==set(objects) and all(len(parts[i])==(sz+CHUNK-1)//CHUNK
              and all(c.HEX.fullmatch(h) for h in parts[i]) for i,sz in objects.items()),'invalid chunk inventory')
    c.require(inventory.cache.hash_value(index)==entry['index_sha256'] and index['actions']=={},'unexpected native index')
    c.atomic_json(stage/'selection.json',{'plan':planned,'parts':parts,'index':index,'native':native})
    needed=[];retained=[]
    for ident,size in planned['missing'].items():
        for pos,h in enumerate(parts[ident]):
            path=c.safe_path(stage/'chunks'/ident/str(pos))
            if path.exists():
                c.require(path.stat().st_size==min(CHUNK,size-pos*CHUNK) and c.digest(path)==h,'invalid retained chunk')
                retained.append([ident,pos])
            else:needed.append([ident,pos])
    return {'needed':needed,'retained':retained}


def put(stage,ident,pos,wire,*,reserve=20*c.GIB):
    stage=c.safe_path(stage);s=c.read_json(stage/'selection.json');size=s['plan']['missing'][ident]
    c.require(type(pos) is int and 0<=pos<len(s['parts'][ident]) and len(wire)<=2*CHUNK,'unselected chunk')
    path=c.safe_path(stage/'chunks'/ident/str(pos))
    c.require(not path.exists(),'chunk already received; reconcile without resending')
    packed=base64.b64decode(wire,validate=True); dec=zlib.decompressobj()
    want=min(CHUNK,size-pos*CHUNK);data=dec.decompress(packed,want+1)
    c.require(len(data)==want and dec.eof and not dec.unused_data and not dec.unconsumed_tail,'invalid chunk frame')
    c.atomic_file(path,io.BytesIO(data),want,s['parts'][ident][pos],reserve=reserve)
    return {'object':ident,'part':pos,'payload':want,'encoded_bytes':len(wire)}


def finish(shared,stage,entry,*,reserve=20*c.GIB):
    shared=c.safe_path(shared);stage=c.safe_path(stage);s=c.read_json(stage/'selection.json');p=s['plan']
    c.require(inventory.plan(shared,entry,s['native'])==p,'target inventory changed; reconcile')
    private=stage/'candidate';mid=next(iter(entry['native_manifests'].values()))
    for ident,size in (p['present']|p['missing']).items():
        dest=private/'objects'/ident[:2]/ident
        if ident in p['present']:
            with c.safe_open(shared/'objects'/ident[:2]/ident) as src:c.atomic_file(dest,src,size,ident,reserve=reserve)
        else:
            # Bounded single object; never build a tar of the base cache.
            c.require(size<=LIMIT,'native object exceeds budget')
            value=bytearray()
            for pos,h in enumerate(s['parts'][ident]):
                with c.safe_open(stage/'chunks'/ident/str(pos)) as src:part=src.read(CHUNK+1)
                c.require(sha(part)==h and len(part)==min(CHUNK,size-pos*CHUNK),'unverified or incomplete frame')
                value.extend(part)
            c.atomic_file(dest,io.BytesIO(value),size,ident,reserve=reserve)
    c.atomic_json(private/'native'/(mid+'.json'),s['native']);c.atomic_json(private/'index.json',s['index'])
    c.atomic_json(private/'validated-bundle.json',{'index_sha256':entry['index_sha256'],'manifest_sha256':entry['manifest_sha256'],
        'producer_id':entry['producer']['id'],'artifact_id':entry['artifact']['id'],'object_count':p['total_objects'],
        'new_object_bytes':p['payload_bytes']+p['reused_bytes']})
    return {'phase':'stage','success':True,'compiler_invocations':0,'shared_cache_modified':False,'index_sha256':entry['index_sha256'],
            'missing_object_bytes':p['payload_bytes'],'node_reused_bytes':p['reused_bytes'],'missing_objects':len(p['missing']),
            'reused_objects':len(p['present']),'base_objects_transmitted':0}
