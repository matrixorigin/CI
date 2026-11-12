import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class NativeParallelTest(unittest.TestCase):
    def test_bounded_parallelism_and_required_headers(self):
        script = Path(__file__).with_name("prepare_coverage_cgo.sh")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "bin"
            binary.mkdir()
            make = binary / "make"
            make.write_text("""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$MAKE_LOG"
if [ "$1" = cgo ]; then
  mkdir -p thirdparties/install/include
  touch thirdparties/install/include/{xxhash,roaring,usearch}.h
fi
""")
            make.chmod(0o755)
            env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                   "MAKE_LOG": str(root / "make.log")}
            subprocess.run(["bash", str(script)], cwd=root, env=env, check=True)
            self.assertEqual((root / "make.log").read_text().splitlines(),
                             ["clean", "config", "cgo NATIVE_BUILD_JOBS=2"])


if __name__ == "__main__":
    unittest.main()
