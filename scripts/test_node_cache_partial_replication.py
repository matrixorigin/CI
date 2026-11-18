import io
import unittest
from unittest.mock import patch

import incremental_cache as c
import node_cache_partial_replication as p
import node_cache_replication as r
import test_node_cache_replication as fixtures


class PartialReplication(unittest.TestCase):
    environment='jed'
    setUp=fixtures.Replication.setUp
    target=fixtures.Replication.target
    bundle=fixtures.Replication.bundle
    rewrite=fixtures.Replication.rewrite

    def partial(self):
        data=self.rewrite(lambda rows:[x for x in rows if x[0].name!=p.tool_name(self.lock)])
        dest=self.root/'partial';dest.mkdir();target=self.target(env='jed')
        with self.assertRaises(ValueError):r.receive(dest,self.catalog,self.source,self.lock,target,io.BytesIO(data))
        return dest,target

    def test_only_missing_tool_transferred_existing_objects_unchanged(self):
        dest,target=self.partial();obj=dest/'.replication-stage'/self.object_name
        before=(obj.stat().st_ino,obj.read_bytes(),obj.stat().st_mtime_ns)
        stream=io.BytesIO();p.export_tool(self.source_root,self.catalog,self.source,self.lock,stream)
        self.assertEqual(len(stream.getvalue()),self.lock['archive_size'])
        stream.seek(0);result=p.finish_tool(dest,self.catalog,self.source,self.lock,target,stream)
        obj=dest/self.object_name
        self.assertEqual(before,(obj.stat().st_ino,obj.read_bytes(),obj.stat().st_mtime_ns))
        self.assertTrue(result['success']);self.assertEqual(result['files'],3)
        self.assertEqual(result['compiler_invocations'],0)
        with self.assertRaises(ValueError):p.finish_tool(dest,self.catalog,self.source,self.lock,target,io.BytesIO(stream.getvalue()))

    def test_wrong_existing_hash_refused_before_tool_write(self):
        dest,target=self.partial();obj=dest/'.replication-stage'/self.object_name
        obj.chmod(0o644);obj.write_bytes(b'X'*obj.stat().st_size)
        with self.assertRaises(ValueError):p.finish_tool(dest,self.catalog,self.source,self.lock,target,io.BytesIO(b'tool'))
        self.assertFalse((dest/'.replication-stage'/p.tool_name(self.lock)).exists())

    def test_extra_missing_file_and_extra_file_refused(self):
        dest,target=self.partial();obj=dest/'.replication-stage'/self.object_name;raw=obj.read_bytes();obj.unlink()
        with self.assertRaises(ValueError):p.inspect(dest,self.catalog,self.source,self.lock,target)
        obj.write_bytes(raw);(dest/'.replication-stage'/'unexpected').write_bytes(b'x')
        with self.assertRaises(ValueError):p.inspect(dest,self.catalog,self.source,self.lock,target)

    def test_bad_tool_retains_stage_and_does_not_publish(self):
        dest,target=self.partial()
        with self.assertRaises(ValueError):p.finish_tool(dest,self.catalog,self.source,self.lock,target,io.BytesIO(b'truncated'))
        self.assertTrue((dest/'.replication-stage'/self.object_name).exists())
        self.assertFalse((dest/'incremental-v2').exists())

    def test_partial_reserve_and_cross_environment_refused(self):
        dest,target=self.partial()
        with patch.object(p.shutil,'disk_usage',return_value=type('Space',(),{'free':20*c.GIB})()):
            with self.assertRaises(ValueError):p.inspect(dest,self.catalog,self.source,self.lock,target)
        target['environment']='mty'
        with self.assertRaises(ValueError):p.inspect(dest,self.catalog,self.source,self.lock,target)

    def test_symlink_refused(self):
        dest,target=self.partial();stage=dest/'.replication-stage'
        (stage/'link').symlink_to(self.source_root/self.object_name)
        with self.assertRaises(ValueError):p.inspect(dest,self.catalog,self.source,self.lock,target)

    def test_wrong_tool_can_retry_without_recopying_existing_files(self):
        dest,target=self.partial();obj=dest/'.replication-stage'/self.object_name
        before=(obj.stat().st_ino,obj.stat().st_mtime_ns,obj.read_bytes())
        with self.assertRaises(ValueError):p.finish_tool(dest,self.catalog,self.source,self.lock,target,io.BytesIO(b'bad'))
        stream=io.BytesIO();p.export_tool(self.source_root,self.catalog,self.source,self.lock,stream);stream.seek(0)
        self.assertTrue(p.finish_tool(dest,self.catalog,self.source,self.lock,target,stream)['success'])
        obj=dest/self.object_name
        self.assertEqual(before,(obj.stat().st_ino,obj.stat().st_mtime_ns,obj.read_bytes()))

    def test_framed_tool_is_length_bounded_and_rejects_missing_footer(self):
        dest,target=self.partial();out=io.BytesIO()
        p.export_tool(self.source_root,self.catalog,self.source,self.lock,out,framed=True)
        out.seek(0);self.assertTrue(p.finish_tool(dest,self.catalog,self.source,self.lock,target,out,framed=True)['success'])

    def test_missing_frame_footer_never_publishes(self):
        dest,target=self.partial();out=io.BytesIO()
        p.export_tool(self.source_root,self.catalog,self.source,self.lock,out,framed=True)
        with self.assertRaises(ValueError):p.finish_tool(dest,self.catalog,self.source,self.lock,target,io.BytesIO(out.getvalue()[:-1]),framed=True)
        self.assertFalse((dest/'incremental-v2').exists());self.assertTrue((dest/'.replication-stage'/self.object_name).exists())
