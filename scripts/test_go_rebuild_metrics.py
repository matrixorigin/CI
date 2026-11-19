import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from go_rebuild_metrics import summarize, run_measured


class RebuildTest(unittest.TestCase):
    def test_real_cold_then_warm_then_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {**os.environ, 'GOCACHE': str(root/'cache'), 'GOENV':'off',
                   'GOTOOLCHAIN':'local','GOPROXY':'off','GOWORK':'off',
                   'GOFLAGS':'','CGO_ENABLED':'0','GOCACHEPROG':''}
            (root/'go.mod').write_text('module example.invalid/rebuild\n\ngo 1.22\n')
            src = root/'value.go'
            src.write_text('package value\nfunc Value() int { return 1 }\n')
            (root/'value_test.go').write_text('package value\nimport "testing"\nfunc TestValue(t *testing.T){ if Value()<1 {t.Fail()} }\n')
            td = subprocess.check_output(['go','env','GOTOOLDIR'],env=env,cwd=root,text=True).strip()
            def run(i, trace=True):
                path = root/f'graph{i}.json'
                cover = root/f'profile{i}.out'
                subprocess.run(['go','test','-mod=readonly','-count=1','-p','2',
                                '-covermode=set','-coverpkg=./...',f'-coverprofile={cover}',
                                *([f'-debug-actiongraph={path}'] if trace else []),'.'],
                               cwd=root,env=env,check=True,stdout=subprocess.DEVNULL)
                return summarize(json.loads(path.read_bytes()),td) if trace else None
            cold, warm = run(0), run(1)
            src.write_text('package value\nfunc Value() int { return 2 }\n')
            changed = run(2)
            self.assertGreater(cold['go_compiler_invocations'],0)
            self.assertEqual(warm['go_compiler_invocations'],0)
            self.assertGreater(changed['go_compiler_invocations'],0)
            self.assertLess(changed['go_compiler_invocations'],cold['go_compiler_invocations'])
            src.write_text('package value\nfunc Value() int { return 3 }\n')
            run(3,trace=False)
            self.assertEqual(run(4)['go_compiler_invocations'],0)
            self.assertEqual((root/'profile3.out').read_bytes(),(root/'profile4.out').read_bytes())
            old=Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ,env,clear=True), (root/'diag').open('wb') as diag:
                    summary=root/'counts.json'
                    result=run_measured(['go','test','-mod=readonly','-count=1','-p','2',
                        '-covermode=set','-coverpkg=./...',f'-coverprofile={root}/profile5.out','.'],
                        diagnostics=diag,output=summary)
                    self.assertEqual(result.returncode,0)
                    self.assertEqual(json.loads(summary.read_bytes())['go_compiler_invocations'],0)
                    self.assertEqual((root/'profile5.out').read_bytes(),(root/'profile4.out').read_bytes())
                    self.assertEqual(list(root.glob('.go-action-*')),[])
            finally:
                os.chdir(old)
            print('go-test compile invocations: cold / warm / source edit:',
                  *(r['go_compiler_invocations'] for r in (cold,warm,changed)))

if __name__=='__main__': unittest.main()
