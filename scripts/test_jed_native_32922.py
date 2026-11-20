import unittest
from unittest.mock import patch

import jed_native_32922 as j


class ExactCandidate(unittest.TestCase):
    def test_only_observed_profile_and_producer(self):
        inp = {str(i):'a'*64 for i in range(196)}
        runtime = {'tool':'compiler'}
        base = (j.SOURCE,runtime,j.KEY,inp,dict(j.TARGETS),'mo-standard-0')
        with patch.object(j,'RUNTIME',j.n.hash_value(runtime)), patch.object(j,'INPUTS',j.n.hash_value(inp)):
            j.validate(*base)
            for idx, bad in enumerate(('a'*40, {}, '3eba204b'+'0'*56, {**inp,'0':'b'*64},
                                       {**j.TARGETS,'mo-standard-1':'different'}, 'mo-standard-1')):
                with self.subTest(field=idx), self.assertRaises(ValueError):
                    j.validate(*(base[:idx]+(bad,)+base[idx+1:]))
