import unittest
from check_buildcache_versions import verify_arm, verify_pair, source_constant, ARM

S, A, H = 'a' * 40, 'b' * 40, 'c' * 40


class Versions(unittest.TestCase):
    def setUp(self):
        self.arm = f'''repository: matrixorigin/matrixone
          ref: {S}
          EXPECTED_HEAD_SHA: {S}
      - name: Checkout isolated build-cache helper
        with:
          repository: matrixorigin/CI
          ref: {H}
'''
        self.files = {(A, ARM): self.arm,
                      (H, 'scripts/compile_cache_probe.py'): f'SOURCE = "{S}"\n',
                      (H, 'scripts/coverage_build_cache.py'): 'from compile_cache_probe import SOURCE\n'}
        self.pair = f'''      - name: Checkout immutable version graph
        with:
          ref: {A}
          python3 scripts/check_buildcache_versions.py --arm-ref {A} --expected-source {S}
  validate-cleanup:
    needs: [validate-versions]
          repository: matrixorigin/matrixone
          ref: {S}
    uses: matrixorigin/CI/{ARM}@{A}
    uses: matrixorigin/CI/{ARM}@{A}
              assert m['source_sha']=='{S}'
'''

    def verify(self):
        return verify_arm(lambda ref, path: self.files[ref, path], A, S)

    def test_valid_immutable_graph(self):
        self.assertEqual(self.verify()['helper'], H)
        verify_pair(self.pair, A, S)

    def test_stale_pinned_helper_rejected(self):
        self.files[H, 'scripts/compile_cache_probe.py'] = 'SOURCE = "' + 'd' * 40 + '"'
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.verify()

    def test_arm_checkout_or_merge_mismatch_rejected(self):
        for marker in ('ref: ', 'EXPECTED_HEAD_SHA: '):
            self.files[A, ARM] = self.arm.replace(marker + S, marker + 'd' * 40)
            with self.assertRaises(ValueError):
                self.verify()

    def test_missing_helper_commit_rejected(self):
        del self.files[H, 'scripts/compile_cache_probe.py']
        with self.assertRaises(KeyError):
            self.verify()

    def test_pair_mismatches_rejected(self):
        for text in (self.pair.replace('@' + A, '@' + 'd' * 40, 1),
                     self.pair.replace("=='" + S, "=='" + 'd' * 40),
                     self.pair.replace('--expected-source ' + S, '--expected-source ' + 'd' * 40),
                     self.pair.replace('needs: [validate-versions]', 'needs: []'),
                     self.pair.replace('ref: ' + S, 'ref: ' + 'd' * 40)):
            with self.assertRaises(ValueError):
                verify_pair(text, A, S)

    def test_missing_duplicate_or_dynamic_source_rejected(self):
        for text in ('', 'SOURCE = get_source()', f'SOURCE="{S}"\nSOURCE="{S}"'):
            with self.assertRaises((ValueError, TypeError)):
                source_constant(text)


if __name__ == '__main__':
    unittest.main()
