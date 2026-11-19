import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import node_cache_pvc_review_control as review


class ReviewScope(unittest.TestCase):
    def test_existing_intent_and_foreign_environment_stop_before_source_or_cluster(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(review.subprocess,'check_output') as source:
            for env in ('ruh','other'):
                with self.assertRaises(ValueError):review.run({'environment':env},{},{},{},{},'unused',Path(tmp),Path(tmp),None)
            source.assert_not_called()

    def test_wrong_scanner_never_reads_credential_or_cluster(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(review.subprocess,'check_output',return_value='wrong'), \
                patch.object(review.control,'Session') as cluster:
            with self.assertRaises(ValueError):review.run({'environment':'ruh'},{},{},{},{},'unused',Path(tmp)/'new',Path(tmp),None)
            cluster.assert_not_called()


if __name__=='__main__':unittest.main()
