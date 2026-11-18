import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import yaml
import build_node_consumer_workflow as workflow
import node_cache_toolchain as tools


class ConsumerContract(unittest.TestCase):
    def test_no_precompile_or_publish_but_original_ut_preserved(self):
        doc = workflow.build('a'*40)
        base = yaml.safe_load((workflow.ROOT/'.github/workflows/node-cache-full-acceptance.yaml').read_text())
        steps = doc['jobs']['ut-coverage-linux-x86']['steps']
        old = next(s for s in base['jobs']['ut-coverage-linux-x86']['steps'] if s.get('id')=='coverage_ut')
        new = next(s for s in steps if s.get('id')=='coverage_ut')
        self.assertNotIn('node_cache_candidate.py', new['run'])
        self.assertNotIn('incremental_candidate', new['run'])
        prefix = old['run'].split("if [ '${{ inputs.node_cache_candidate }}' = true ]; then")[0]
        suffix = old['run'][old['run'].index('# Fail early on the real'):]
        self.assertEqual(new['run'], prefix+suffix)
        self.assertEqual(old['env'], new['env'])
        self.assertFalse(any(s.get('id')=='upload_node_cache_candidate' for s in steps))
        for ident in ('node_cache','node_cache_acceptance'):
            a = next(s for s in steps if s.get('id')==ident)
            b = next(s for s in base['jobs']['ut-coverage-linux-x86']['steps'] if s.get('id')==ident)
            self.assertEqual(a,b)
        self.assertEqual(doc['jobs']['coverage-final-status'],base['jobs']['coverage-final-status'])

    def test_two_groups_and_producer_rejected(self):
        doc = workflow.build('a'*40)
        strategy=doc['jobs']['ut-coverage-linux-x86']['strategy']
        self.assertEqual(strategy['matrix']['group'],[0,1])
        self.assertEqual(strategy['max-parallel'],2)
        shell=doc['jobs']['check_coverage_eligibility']['steps'][-1]['run']
        self.assertIn('test "$CANDIDATE" = false',shell)
        self.assertIn('test "$GROUP_COUNT" = 2',shell)
        self.assertFalse(doc['on']['workflow_call']['inputs']['node_cache_candidate']['default'])


class ToolTransport(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.stage=self.root/'stage'
        hashes={}
        for name in tools.VERSIONS:
            path=self.stage/name/'bin'/name;path.parent.mkdir(parents=True)
            path.write_bytes(name.encode());hashes[name]=tools.digest(path)
        self.catalog=self.root/'catalog.json'
        self.catalog.write_text(json.dumps({'entries':[{'runtime':{'tool_binaries':hashes}}]}))
        self.archive=self.root/'tools.tar.gz'
        self.sha=tools.pack(self.stage,self.archive,self.catalog)
        self.space=patch.object(tools.shutil,'disk_usage',return_value=type('Space',(),{'free':100*1024**3})())
        self.space.start();self.addCleanup(self.space.stop)

    def test_roundtrip(self):
        dest=self.root/'restored';tools.unpack(self.archive,self.sha,dest,self.catalog)
        tools.check(dest,tools.expected(self.catalog))

    def test_bad_digest_stops_before_creation(self):
        dest=self.root/'restored'
        with self.assertRaises(ValueError):tools.unpack(self.archive,'0'*64,dest,self.catalog)
        self.assertFalse(dest.exists())

    def test_wrong_binary_rejected(self):
        (self.stage/'go/bin/go').write_bytes(b'wrong')
        with self.assertRaises(ValueError):tools.pack(self.stage,self.root/'bad.tar.gz',self.catalog)

    def test_traversal_links_and_special_files_rejected(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('go/link',tarfile.SYMTYPE),('go/dev',tarfile.CHRTYPE)]:
            archive=self.root/'bad.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                info=tarfile.TarInfo(name);info.type=kind;info.linkname='/tmp/outside';tar.addfile(info,io.BytesIO(b''))
            dest=self.root/'bad-output'
            with self.assertRaises(ValueError):tools.unpack(archive,tools.digest(archive),dest,self.catalog)
            self.assertFalse(dest.exists())

    def test_space_guard_and_overwrite(self):
        dest=self.root/'restored'
        with patch.object(tools.shutil,'disk_usage',return_value=type('Space',(),{'free':1})()):
            with self.assertRaises(ValueError):tools.unpack(self.archive,self.sha,dest,self.catalog)
        dest.mkdir()
        with self.assertRaises(ValueError):tools.unpack(self.archive,self.sha,dest,self.catalog)


if __name__=='__main__':unittest.main()
