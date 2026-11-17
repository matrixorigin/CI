import copy
import unittest
from unittest.mock import patch

import incremental_cache as c
import node_cache_append as a
import test_node_cache_publish_job as fixtures


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PublisherJobTests('test_initial_publish_and_independent_verify')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.shared=self.f.f.shared
        self.binding=self.f.request['binding']
        c.atomic_json(self.shared/'node-cache-identity.json',self.binding)
        self.base={'binding':self.binding,'marker_sha256':c.digest(self.shared/'node-cache-identity.json'),
                   'indexes':self.f.f.cfg['indexes'],'native_manifests':[]}
        self.before={str(p.relative_to(self.shared)):c.digest(p) for p in self.shared.rglob('*') if p.is_file()}

    def invoke(self,base=None):
        return a.append(self.f.stage/'candidate',self.shared,self.f.entry,
                        self.base if base is None else base,self.f.request)

    def assert_old_unchanged(self):
        for name,digest in self.before.items():self.assertEqual(c.digest(self.shared/name),digest)

    def test_append_new_objects_without_recompilation_or_old_mutation(self):
        report=self.invoke()
        self.assertEqual(report['new_bytes'],len(b'only changed package'))
        self.assertEqual(report['compiler_invocations'],0)
        self.assertTrue(report['independently_verified']);self.assert_old_unchanged()

    def test_stale_authorization_rejected_before_write(self):
        self.f.request['checked_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(ValueError):self.invoke()
        self.assert_old_unchanged()
        self.assertFalse((self.shared/'indexes'/(self.f.entry['index_sha256']+'.json')).exists())

    def test_wrong_volume_binding_and_marker_refused(self):
        base=copy.deepcopy(self.base);base['binding']['pvc_uid']='4'*8+'-'+'4'*4+'-'+'4'*4+'-'+'4'*4+'-'+'4'*12
        with self.assertRaises(ValueError):self.invoke(base)
        base=copy.deepcopy(self.base);base['marker_sha256']='0'*64
        with self.assertRaises(ValueError):self.invoke(base)
        self.assert_old_unchanged()

    def test_unreviewed_parent_index_refused(self):
        c.atomic_json(self.shared/'indexes'/('f'*64+'.json'),{'unexpected':True})
        with self.assertRaises(ValueError):self.invoke()
        self.assert_old_unchanged()

    def test_corrupt_parent_object_refused_before_candidate_publish(self):
        obj=self.shared/'objects'/self.f.f.old_id[:2]/self.f.f.old_id
        obj.chmod(0o600);obj.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.invoke()
        self.assertFalse((self.shared/'indexes'/(self.f.entry['index_sha256']+'.json')).exists())

    def test_conflicting_action_rejected(self):
        parent={'schema':2,'runtime':self.f.entry['runtime'],
                'actions':{'e'*64:{'output_id':self.f.f.old_id,'size':len(self.f.f.old)}}}
        import hashlib
        ident=hashlib.sha256(c.canonical(parent)).hexdigest()
        c.atomic_json(self.shared/'indexes'/(ident+'.json'),parent)
        base={**self.base,'indexes':self.base['indexes']+[ident]}
        with self.assertRaisesRegex(ValueError,'append action conflict'):self.invoke(base)
        self.assert_old_unchanged()

    def test_completed_append_not_automatically_retried(self):
        self.invoke()
        with self.assertRaises(ValueError):self.invoke()
        base={**self.base,'indexes':self.base['indexes']+[self.f.entry['index_sha256']]}
        with self.assertRaisesRegex(ValueError,'already published'):self.invoke(base)
        self.assert_old_unchanged()

    def test_base_rechecked_under_publication_lock(self):
        real=a.verify_base;calls=[]
        def guard(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise ValueError('simulated changed approval at lock')
            return real(*args,**kwargs)
        with patch.object(a,'verify_base',side_effect=guard):
            with self.assertRaisesRegex(ValueError,'at lock'):self.invoke()
        self.assertEqual(len(calls),2);self.assert_old_unchanged()
        self.assertFalse((self.shared/'indexes'/(self.f.entry['index_sha256']+'.json')).exists())

    def test_corrupt_delta_cannot_change_existing_tree(self):
        obj=next((self.f.stage/'candidate/objects').glob('*/*'))
        obj.chmod(0o600);obj.write_bytes(b'bad-delta')
        with self.assertRaises(ValueError):self.invoke()
        self.assert_old_unchanged()
        self.assertFalse((self.shared/'indexes'/(self.f.entry['index_sha256']+'.json')).exists())


if __name__=='__main__':unittest.main()
