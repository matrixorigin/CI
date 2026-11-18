import io
import unittest
from unittest.mock import patch

import node_cache_chunk_replication as q
import test_node_cache_replication as f


class Chunks(unittest.TestCase):
    setUp=f.Replication.setUp
    target=f.Replication.target

    def test_bounded_batches_and_final_independent_hashes(self):
        dest=self.root/'target';dest.mkdir();identity=self.target()
        with patch.object(q,'BUDGET',1):
            plan=q.r.make_plan(self.source_root,self.catalog,self.source,self.lock)
            expected,groups=q.batches(plan,self.catalog,self.source,self.lock)
            self.assertEqual(len(groups),len(expected))
            for i in range(len(groups)):
                stream=io.BytesIO();q.export_batch(self.source_root,self.catalog,self.source,self.lock,i,stream);stream.seek(0)
                result=q.receive_batch(dest,self.catalog,self.source,self.lock,identity,i,stream)
                self.assertEqual(result['number'],i);self.assertFalse((dest/'incremental-v2').exists())
            result=q.finish(dest,self.catalog,self.source,self.lock,identity)
            self.assertEqual(result['files'],len(expected));self.assertTrue(result['success'])

    def test_incomplete_batch_retains_verified_files_and_only_failed_batch_can_resume(self):
        dest=self.root/'target';dest.mkdir();identity=self.target()
        stream=io.BytesIO();q.export_batch(self.source_root,self.catalog,self.source,self.lock,0,stream)
        raw=stream.getvalue()
        with q.tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            cut=archive.getmembers()[2].offset_data+1
        with self.assertRaises((ValueError,EOFError,q.tarfile.ReadError)):
            q.receive_batch(dest,self.catalog,self.source,self.lock,identity,0,io.BytesIO(raw[:cut]))
        self.assertTrue((dest/'.replication-stage').exists());self.assertFalse((dest/'incremental-v2').exists())
        result=q.receive_batch(dest,self.catalog,self.source,self.lock,identity,0,io.BytesIO(raw))
        self.assertTrue(result['success'])
        with self.assertRaises(ValueError):q.receive_batch(dest,self.catalog,self.source,self.lock,identity,0,io.BytesIO(raw))

    def test_missing_batch_and_changed_existing_hash_fail_publication(self):
        dest=self.root/'target';dest.mkdir();identity=self.target()
        with patch.object(q,'BUDGET',1):
            stream=io.BytesIO();q.export_batch(self.source_root,self.catalog,self.source,self.lock,0,stream);stream.seek(0)
            q.receive_batch(dest,self.catalog,self.source,self.lock,identity,0,stream)
            with self.assertRaises((ValueError,FileNotFoundError)):q.finish(dest,self.catalog,self.source,self.lock,identity)
        self.assertFalse((dest/'incremental-v2').exists())

    def test_wrong_target_and_extra_payload_never_publish(self):
        dest=self.root/'target';dest.mkdir();identity=self.target(env='jed')
        stream=io.BytesIO();q.export_batch(self.source_root,self.catalog,self.source,self.lock,0,stream);stream.seek(0)
        with self.assertRaises(ValueError):q.receive_batch(dest,self.catalog,self.source,self.lock,identity,0,stream)
        self.assertFalse((dest/'.replication-stage').exists())
