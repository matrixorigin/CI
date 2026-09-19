#!/usr/bin/env python3
"""Opt-in real-Go path-contract experiment. No network or MatrixOne build.

Runs cold/matching/checkout-relocated/module-cache-relocated race tests and
reports actual compile commands plus uncached test-body execution. This is a
mechanism probe, not an end-to-end MatrixOne/Linux performance benchmark.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile


def main():
    with tempfile.TemporaryDirectory(prefix="go-seed-path-probe-") as temporary:
        root = Path(temporary).resolve()
        proxy = root / "proxy/example.test/dependency/@v"
        proxy.mkdir(parents=True)
        mod = "module example.test/dependency\n\ngo 1.23\n"
        (proxy / "v1.0.0.mod").write_text(mod)
        (proxy / "v1.0.0.info").write_text('{"Version":"v1.0.0","Time":"2026-01-01T00:00:00Z"}')
        with zipfile.ZipFile(proxy / "v1.0.0.zip", "w") as archive:
            archive.writestr("example.test/dependency@v1.0.0/go.mod", mod)
            archive.writestr("example.test/dependency@v1.0.0/dep.go", "package dependency\nfunc Value() int { return 42 }\n")
        checkout = root / "checkout"
        checkout.mkdir()
        (checkout / "go.mod").write_text("module example.test/cacheprobe\n\ngo 1.23\nrequire example.test/dependency v1.0.0\n")
        (checkout / "probe_test.go").write_text('''package cacheprobe
import ("testing"; "fmt"; "example.test/dependency")
func TestBody(t *testing.T) {
 if dependency.Value() != 42 { t.Fatal("bad value") }
 fmt.Println("UNCACHED_TEST_BODY_EXECUTED")
}
''')
        env = dict(os.environ, GOCACHE=str(root / "cache"), GOMODCACHE=str(root / "modules"),
                   GOPROXY=(root / "proxy").as_uri(), GOSUMDB="off", GOWORK="off", GOFLAGS="",
                   GOTOOLCHAIN="local")
        subprocess.run(["go", "mod", "download", "example.test/dependency"], cwd=checkout,
                       env=env, check=True, timeout=60)
        results = {}

        def run(name, cwd, modules):
            started = time.monotonic()
            result = subprocess.run(["go", "test", "-race", "-count=1", "-vet=off", "-x", "-v", "./..."],
                                    cwd=cwd, env=dict(env, GOMODCACHE=str(modules)),
                                    capture_output=True, text=True, timeout=120, check=True)
            commands = [line for line in result.stderr.splitlines() if re.search(r"/compile(?: |$)", line)]
            assert "UNCACHED_TEST_BODY_EXECUTED" in result.stdout
            results[name] = {"seconds": round(time.monotonic() - started, 3),
                             "compiler_invocations": len(commands),
                             "dependency_compiles": sum("-p example.test/dependency " in s for s in commands),
                             "checkout_compiles": sum("-p example.test/cacheprobe " in s for s in commands),
                             "test_body_executed": True}

        run("cold", checkout, root / "modules")
        run("matching", checkout, root / "modules")
        moved = root / "relocated-checkout"
        shutil.copytree(checkout, moved)
        run("checkout_relocated", moved, root / "modules")
        shutil.copytree(root / "modules", root / "relocated-modules")
        run("modules_relocated", checkout, root / "relocated-modules")
        assert results["matching"]["compiler_invocations"] == 0, results
        assert results["checkout_relocated"]["checkout_compiles"] > 0, results
        assert results["checkout_relocated"]["dependency_compiles"] == 0, results
        assert results["modules_relocated"]["dependency_compiles"] > 0, results
        print(json.dumps({"go": subprocess.check_output(["go", "version"], text=True).strip(),
                          "results": results}, indent=2))


if __name__ == "__main__":
    main()
