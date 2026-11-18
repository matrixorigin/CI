import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import node_cache_workspace_probe_job as q

NONCE='00000000-0000-4000-8000-000000000001'


class ProbeJob(unittest.TestCase):
    def exercise(self,fault=None):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();shared=root/'cache';work=root/'work';tmp=work/'tmp'
            shared.mkdir();tmp.mkdir(parents=True);payload=shared/'object';payload.write_bytes(b'cache')
            wanted=q.c.digest(payload);before=(payload.stat().st_ino,payload.stat().st_mtime_ns,payload.read_bytes())
            entry={'runtime':{'cpu':{}}};cpu={} if fault!='cpu' else {'model':'different'}
            denied=errno.EACCES if fault=='errno' else errno.EROFS
            original_open=q.os.open;write_attempts=[]
            def boundary_open(path,flags,mode=0o777):
                if flags & q.os.O_CREAT:
                    write_attempts.append(str(path));raise OSError(denied,'denied')
                return original_open(path,flags,mode)
            versions=['go version go1.27.1 linux/amd64','cmake version 3.17.2\n']
            if fault=='version':versions[0]='go version wrong'
            with patch.object(q.c,'read_json',return_value={}),patch.object(q.tools,'binding',return_value={'node':'node'}), \
                patch.object(q.cache,'catalog_for_node',return_value={'entries':[entry]}), \
                patch.object(q,'cpu_profile',return_value=cpu),patch.object(q.replication,'make_plan',return_value={}), \
                patch.object(q.replication,'expected_files',return_value={'object':(5,'0'*64 if fault=='hash' else wanted)}), \
                patch.object(q.os,'open',side_effect=boundary_open), \
                patch.object(q.tools,'restore',return_value={'external_tool_downloads':0,'persistent_tools':True}) as restore, \
                patch.object(q.subprocess,'check_output',side_effect=versions) as executed:
                if fault:
                    with self.assertRaises(ValueError):q.run(shared,root/'catalog',root/'lock','mty','node',{},work,tmp,NONCE)
                    if fault in ('cpu','hash'):self.assertEqual(write_attempts,[]);restore.assert_not_called()
                else:
                    result=q.run(shared,root/'catalog',root/'lock','mty','node',{},work,tmp,NONCE)
                    self.assertTrue(result['success']);self.assertEqual(result['compiler_invocations'],0)
                    self.assertEqual(result['external_tool_downloads'],0);self.assertEqual(executed.call_count,2)
                    self.assertEqual(executed.call_args_list[0].kwargs['env']['GOTOOLCHAIN'],'local')
                    self.assertEqual(executed.call_args_list[0].kwargs['env']['GOPROXY'],'off')
                    q.check_private(work,tmp,NONCE)
            self.assertEqual(before,(payload.stat().st_ino,payload.stat().st_mtime_ns,payload.read_bytes()))

    def test_readonly_payload_and_private_writes(self):self.exercise()
    def test_cpu_mismatch_no_cache_or_workspace_write(self):self.exercise('cpu')
    def test_hash_mismatch_no_restore(self):self.exercise('hash')
    def test_permission_error_is_not_readonly_mount_proof(self):self.exercise('errno')
    def test_restored_version_mismatch_rejected(self):self.exercise('version')
    def test_crossed_private_marker_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            work=Path(raw).resolve();tmp=work/'tmp';tmp.mkdir()
            q.check_private(work,tmp,NONCE,True)
            (work/'.private-workspace-proof').write_bytes(b'other-pod')
            with self.assertRaises(ValueError):q.check_private(work,tmp,NONCE)


if __name__=='__main__':unittest.main()
