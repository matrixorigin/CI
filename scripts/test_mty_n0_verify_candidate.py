import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mty_n0_verify_candidate as v


class CandidateReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(strict=True)
        cat = v.c.read_json(v.pre.AUTH)['base_catalog']
        binding, = [b for b in cat['nodes'] if b['node'] == v.pre.NODE]
        parent = v.n.catalog_for_node(cat, binding)['entries'][0]
        self.entry = copy.deepcopy(parent)
        self.entry.update(source=v.pre.SOURCE, bindings=[], native_manifests={'a'*64:'b'*64})
        self.entry['runtime']['system_packages_sha256'] = 'c'*64
        self.path = self.root/'entry'
        self.work = self.root/'work'

    def run_review(self, receiver):
        v.c.atomic_json(self.path, self.entry)
        return v.verify(self.path, v.c.digest(self.path), self.work, receive=receiver)

    def test_bad_pin_never_stages(self):
        v.c.atomic_json(self.path,self.entry)
        with self.assertRaisesRegex(ValueError,'protected pin'):
            v.verify(self.path,'0'*64,self.work,receive=None)
        self.assertFalse(self.work.exists())

    def test_fork_checkout_never_stages(self):
        self.entry['source'] = v.pre.PR_HEAD
        with self.assertRaisesRegex(ValueError,'trusted baseline'):
            self.run_review(None)
        self.assertFalse(self.work.exists())

    def test_transport_rejection_stops_review(self):
        with self.assertRaisesRegex(ValueError,'corrupt object'):
            self.run_review(Mock(side_effect=ValueError('corrupt object')))

    def test_candidate_receipt_never_authorizes_pvc(self):
        raw = b'index'
        import hashlib
        self.entry['index_sha256'] = hashlib.sha256(raw).hexdigest()
        def receive(entry,dest,stage):
            dest.mkdir(); stage.mkdir()
            v.c.atomic_json(dest/'manifest.json',dict(contract=dict(flags=v.n.legacy.FLAGS),
                source=v.pre.SOURCE,parent_indexes=[],object_count=10))
            (stage/'index.json').write_bytes(raw)
            return stage/'index.json'
        with patch.object(v.prepare,'check_scope') as scope:
            receipt = self.run_review(receive)
        scope.assert_called_once()
        self.assertFalse(receipt['publication_authorized'])
        self.assertFalse(receipt['pvc_verified'])
