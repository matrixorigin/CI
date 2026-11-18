import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import node_cache_validation as v


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)
        self.file=self.source/'source.go';self.file.write_text('// controlled source\n')
        self.policy={'source_shas':['a'*40,'b'*40],'controlled_sources':{'b'*40:{
            'baseline_sha':'a'*40,'files':{'source.go':hashlib.sha256(self.file.read_bytes()).hexdigest()},
            'canary_package':'example/p','canary_test':'TestControlled'}}}
        self.state={'entries':[{'index_sha256':'c'*64,'source':'a'*40,'compile_only':True,
            'native_manifests':{'d'*64:'e'*64}}],'config':{'indexes':['c'*64]},
            'native_key':'d'*64,'native_authorities':{'d'*64:'e'*64}}
        self.native={'native_hit':True,'native_output_sha256':'f'*64}
        self.acceptance={'shared_hits':10,'copied_bytes':100}

    def events(self,actions=('run','pass')):
        return io.StringIO(''.join(json.dumps({'Action':a,'Package':'example/p','Test':'TestControlled'})+'\n' for a in actions))

    def validate(self,sha='b'*40,packages=None,events=None):
        return v.validate(self.policy,self.state,self.native,self.acceptance,sha,self.source,
            packages or ['example/p'],self.events() if events is None else events)

    def test_unchanged_sha_requires_native_and_shared_hits(self):
        self.assertIsNone(self.validate(sha='a'*40)['controlled_source'])

    def test_changed_sha_reuses_baseline_and_executes_canary(self):
        result=self.validate()
        self.assertTrue(result['accepted'])
        self.assertEqual(result['producer_sources'],['a'*40])
        self.assertEqual(result['controlled_source'],{'assigned':True,'runs':1,'passes':1})

    def test_other_group_must_not_execute_canary(self):
        self.assertFalse(self.validate(packages=['example/other'],events=self.events(()))['controlled_source']['assigned'])
        with self.assertRaises(AssertionError):self.validate(packages=['example/other'])

    def test_missing_failed_skipped_or_duplicate_test_rejected(self):
        for actions in ((),('run',),('run','fail'),('run','skip'),('run','pass','run','pass')):
            with self.subTest(actions=actions),self.assertRaises(AssertionError):self.validate(events=self.events(actions))

    def test_native_miss_source_tamper_wrong_sha_and_unproven_generation_rejected(self):
        for target,key,value in ((self.native,'native_hit',False),(self.acceptance,'shared_hits',0),
                                 (self.state,'native_authorities',{})):
            old=copy.deepcopy(target[key]);target[key]=value
            with self.assertRaises(AssertionError):self.validate()
            target[key]=old
        with self.assertRaises(AssertionError):self.validate(sha='f'*40)
        self.file.write_text('// altered\n')
        with self.assertRaises(AssertionError):self.validate()

    def test_nonbaseline_generation_rejected(self):
        self.state['entries'][0]['source']='b'*40
        with self.assertRaises(AssertionError):self.validate()


if __name__=='__main__':unittest.main()
