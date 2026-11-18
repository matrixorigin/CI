import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import incremental_cache as c
import node_cache_toolchain as t
import node_cache_toolchain_store as store
import node_cache_toolchain_publish as pub
import build_node_local_tools_workflow as flow


class PersistentTools(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.shared=self.root/'shared';self.shared.mkdir()
        self.identity={'environment':'mty','node':'mo-standard-0','node_uid':'n','pvc_uid':'p','pv_uid':'v'}
        marker=self.shared/'incremental-v2/node-cache-identity.json';marker.parent.mkdir()
        marker.write_bytes(c.canonical(self.identity))
        self.bound={**self.identity,'marker_sha256':c.digest(marker)}
        self.stage=self.root/'tools';hashes={}
        for tool in t.VERSIONS:
            binary=self.stage/tool/'bin'/tool;binary.parent.mkdir(parents=True)
            binary.write_bytes(tool.encode());hashes[tool]=c.digest(binary)
        self.catalog={'schema':2,'nodes':[self.bound],'entries':[{'runtime':{'tool_binaries':hashes}}]}
        self.catalog_path=self.root/'catalog.json';self.catalog_path.write_text(json.dumps(self.catalog))
        self.archive=self.root/'tools.tar.gz';sha=t.pack(self.stage,self.archive,self.catalog_path)
        self.zip=self.root/'artifact.zip'
        with zipfile.ZipFile(self.zip,'w') as z:z.write(self.archive,'toolchain.tar.gz')
        self.lock={'schema':1,'versions':t.VERSIONS,'archive_sha256':sha,'archive_size':self.archive.stat().st_size,
            'producer':{'repository':'matrixorigin/CI','event':'push','attempt':1,'workflow_path':'.github/workflows/node-cache-canary.yaml',
                        'sha':'a'*40,'id':42,'branch':'codex/tools','workflow_id':23,'actor':'reviewed'},
            'artifact':{'id':9,'name':'reviewed-toolchain-contract-42','size':self.zip.stat().st_size,'digest':'sha256:'+c.digest(self.zip)}}
        self.space=patch.object(t.shutil,'disk_usage',return_value=type('Space',(),{'free':100*c.GIB})())
        self.space.start();self.addCleanup(self.space.stop)

    def publish(self):
        archive=store.receive(self.zip,self.root/'received',self.lock,self.catalog_path)
        return store.publish(self.shared,self.lock,self.bound,archive)

    def test_two_private_readers_and_replacement_preserve_shared(self):
        self.publish();before=c.digest(store.verify(self.shared,self.lock,self.bound))
        with patch.object(store.n,'readonly_mount') as mount:
            for i in range(3):
                dest=self.root/f'runner-{i}'
                report=store.restore(self.shared,self.lock,self.catalog_path,'mty','mo-standard-0',self.identity,dest)
                self.assertEqual(report['external_tool_downloads'],0)
                (dest/'go/bin/go').write_bytes(b'private change')
            self.assertEqual(mount.call_count,3)
        self.assertEqual(before,c.digest(store.verify(self.shared,self.lock,self.bound)))

    def test_duplicate_publication_rejected_and_original_retained(self):
        self.publish()
        with self.assertRaises(ValueError):store.publish(self.shared,self.lock,self.bound,self.archive)
        store.verify(self.shared,self.lock,self.bound)

    def test_archive_corruption_no_fallback(self):
        self.publish();archive=store.path_for(self.shared,self.lock)/'toolchain.tar.gz';archive.write_bytes(b'bad')
        with self.assertRaises(ValueError):store.verify(self.shared,self.lock,self.bound)

    def test_receipt_wrong_binding_or_lock_rejected(self):
        self.publish()
        with self.assertRaises(ValueError):store.verify(self.shared,self.lock,{**self.bound,'pvc_uid':'wrong'})
        wrong=copy.deepcopy(self.lock);wrong['versions']['go']='wrong'
        with self.assertRaises(ValueError):store.verify(self.shared,wrong,self.bound)

    def test_foreign_environment_node_and_uid_rejected(self):
        for env,node,hints in [('ruh','mo-standard-0',self.identity),('mty','mo-standard-1',self.identity),
                               ('mty','mo-standard-0',{**self.identity,'node_uid':'wrong'})]:
            with self.assertRaises(ValueError):store.binding(self.catalog,self.shared,env,node,hints,False)

    def test_symlink_rejected(self):
        (self.shared/'toolchains-v1').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):store.publish(self.shared,self.lock,self.bound,self.archive)

    def test_zip_corrupt_and_extra_member_rejected(self):
        self.zip.write_bytes(b'bad')
        with self.assertRaises(ValueError):store.receive(self.zip,self.root/'received',self.lock,self.catalog_path)
        with zipfile.ZipFile(self.zip,'w') as z:
            z.write(self.archive,'toolchain.tar.gz');z.writestr('extra',b'x')
        lock=copy.deepcopy(self.lock);lock['artifact'].update(size=self.zip.stat().st_size,digest='sha256:'+c.digest(self.zip))
        with self.assertRaises(ValueError):store.receive(self.zip,self.root/'received',lock,self.catalog_path)

    def test_space_guard_before_shared_write(self):
        with patch.object(t.shutil,'disk_usage',return_value=type('Space',(),{'free':1})()):
            with self.assertRaises(ValueError):store.publish(self.shared,self.lock,self.bound,self.archive)
        self.assertFalse((self.shared/'toolchains-v1').exists())

    def test_producer_and_artifact_provenance(self):
        p=self.lock['producer'];a=self.lock['artifact']
        run={'id':p['id'],'head_sha':p['sha'],'head_branch':p['branch'],'event':p['event'],'path':p['workflow_path'],
             'workflow_id':p['workflow_id'],'run_attempt':1,'actor':{'login':p['actor']},'status':'completed',
             'conclusion':'success','repository':{'full_name':p['repository']}}
        artifact={'id':a['id'],'name':a['name'],'size_in_bytes':a['size'],'digest':a['digest'],'expired':False,
                  'workflow_run':{'id':p['id'],'head_sha':p['sha']}}
        store.authorize(self.lock,run,artifact)
        for key,value in [('head_sha','b'*40),('conclusion','failure'),('run_attempt',2),('event','pull_request')]:
            with self.assertRaises(ValueError):store.authorize(self.lock,{**run,key:value},artifact)
        for key,value in [('expired',True),('digest','sha256:'+'0'*64),('id',10)]:
            with self.assertRaises(ValueError):store.authorize(self.lock,run,{**artifact,key:value})


class Workflow(unittest.TestCase):
    def test_publisher_only_new_cm_pod_existing_claim(self):
        policy={'environment':'mty','name':'cs-mty-incremental-n0-v1','node':'mo-standard-0'}
        lock=json.loads((flow.consumer.ROOT/'config/node-toolchain-lock.json').read_text())
        entry={'producer':lock['producer'],'artifact':lock['artifact'],'toolchain':lock,'catalog':{}}
        operation=pub.name(policy,entry);cm,pod=pub.objects(policy,entry,{},operation)
        self.assertEqual([cm['kind'],pod['kind']],['ConfigMap','Pod'])
        self.assertTrue(cm['immutable']);self.assertFalse(pod['spec']['automountServiceAccountToken'])
        self.assertEqual(pod['spec']['volumes'][0]['persistentVolumeClaim']['claimName'],policy['name'])
        self.assertEqual(pod['spec']['volumes'][-1]['configMap']['name'],operation)
        self.assertEqual(pod['spec']['containers'][0]['resources']['limits'],{'cpu':'1','memory':'1Gi'})
        self.assertIn('node_cache_toolchain_job.py',cm['data'])

    def test_only_local_tools_two_groups_no_precompile(self):
        doc=flow.build('a'*40);job=doc['jobs']['ut-coverage-linux-x86']
        self.assertNotIn('prepare-toolchain',doc['jobs'])
        self.assertEqual(job['strategy']['matrix']['group'],[0,1]);self.assertEqual(job['strategy']['max-parallel'],2)
        steps=job['steps'];names=[s.get('name','') for s in steps]
        self.assertNotIn('Download verified toolchain artifact',names)
        restore=next(s for s in steps if s.get('id')=='persistent_toolchain')
        self.assertNotIn('curl',restore['run']);self.assertIn('node_cache_toolchain_store.py',restore['run'])
        ut=next(s for s in steps if s.get('id')=='coverage_ut')
        self.assertNotIn('incremental_candidate',ut['run'])
        old=flow.consumer.build('a'*40)['jobs']['ut-coverage-linux-x86']
        self.assertEqual(ut,next(s for s in old['steps'] if s.get('id')=='coverage_ut'))
        self.assertEqual(job['container'],old['container']) if 'container' in job else None


if __name__=='__main__':unittest.main()
