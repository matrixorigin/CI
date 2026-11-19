import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
import node_cache_native_selective as n


def fixture():
    key='a'*64;runtime={'environment':'fixture'};source='b'*40
    objects={n.sha(b):b for b in [b'a',b'x'*(n.CHUNK+11)]}
    native={'compile_only':True,'runtime':runtime,'key':key,'outputs':{
        f'cgo/{j}.o':{'sha256':i,'size':len(b)} for j,(i,b) in enumerate(objects.items())}}
    mid=n.inventory.cache.hash_value(native)
    index={'schema':2,'actions':{},'source':source,'runtime':runtime,'native_generation':key}
    files={'index.json':n.c.canonical(index),'native/'+mid+'.json':n.c.canonical(native)}
    files.update({'objects/'+i[:2]+'/'+i:b for i,b in objects.items()})
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as t:
        for name,b in files.items():
            member=tarfile.TarInfo(name);member.size=len(b);t.addfile(member,io.BytesIO(b))
    archive=buf.getvalue();entry={'compile_only':True,'native_only':True,'runtime':runtime,'source':source,
        'index_sha256':n.sha(files['index.json']),'native_manifests':{key:mid}}
    m={**entry,'go_precompile':False,'ut_execution':False,'shared_cache_writes':0,'archive_bytes':len(archive),
        'archive_sha256':n.sha(archive),'unpacked_bytes':sum(map(len,files.values())),'object_bytes':sum(map(len,objects.values())),
        'object_count':len(objects)}
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:z.writestr('manifest.json',n.c.canonical(m));z.writestr('delta.tar.gz',archive)
    raw=buf.getvalue()
    entry.update(artifact={'id':11,'size_in_bytes':len(raw),'digest':'sha256:'+n.sha(raw)},producer={'id':22},manifest_sha256=n.inventory.cache.hash_value(m))
    return raw,entry,objects


class DataPlane(unittest.TestCase):
    def exercise(self,raw,entry,objects,present,interrupt=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();shared=root/'shared';stage=root/'own-stage'
            for i in present:
                path=shared/'objects'/i[:2]/i;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(objects[i])
            sent=[];acks=[];stop=interrupt
            def invoke(action,r):
                nonlocal stop
                if action=='inventory':return n.begin(shared,stage,entry,r['index'],r['native'],r['parts'])
                if action=='chunk':
                    self.assertNotIn(r['object'],present)
                    tag=(r['object'],r['part']);self.assertNotIn(tag,sent);sent.append(tag)
                    result=n.put(stage,*tag,r['wire'],reserve=0);acks.append(result)
                    if stop:stop=False;raise ConnectionError('lost response after durable chunk')
                    return result
                self.assertEqual(action,'stage')
                return n.finish(shared,stage,entry,reserve=0)
            if interrupt:
                with self.assertRaises(ConnectionError):n.transfer(entry,raw,invoke,dict)
                self.assertFalse((stage/'candidate'/'validated-bundle.json').exists())
            result=n.transfer(entry,raw,invoke,dict)
            self.assertEqual(result['acknowledged_payload_bytes'],sum(len(b) for i,b in objects.items() if i not in present))
            self.assertEqual(result['node_reused_bytes'],sum(len(objects[i]) for i in present))
            self.assertEqual(result['acknowledged_payload_bytes'],sum(a['payload'] for a in acks))
            for i,data in objects.items():
                self.assertEqual((stage/'candidate'/'objects'/i[:2]/i).read_bytes(),data)
                self.assertEqual((shared/'objects'/i[:2]/i).exists(),i in present)
            return result

    def test_cold_partial_warm_and_lost_ack(self):
        raw,entry,o=fixture();first=next(iter(o))
        for p in (set(),{first},set(o)):
            self.exercise(raw,entry,o,p)
        r=self.exercise(raw,entry,o,{first},True)
        self.assertEqual(r['retained_chunk_bytes'],n.CHUNK)

    def test_wrong_source_rejected_before_target(self):
        raw,e,_=fixture()
        with self.assertRaises(ValueError):n.transfer(e,raw+b'x',lambda *_:self.fail('touched target'),dict)
