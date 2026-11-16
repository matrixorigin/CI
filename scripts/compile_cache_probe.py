#!/usr/bin/env python3
"""One-run, local-only native/Go build-cache probe. Never executes UT binaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

SOURCE = "c129ff5677ff5a215491ba990167e013d79dcfb9"
GIB = 1024**3


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def members_safe(members):
    names = set()
    for member in members:
        path = Path(member.name)
        if (path.is_absolute() or ".." in path.parts or member.name in names
                or not member.isfile() or len(path.parts) < 2
                or path.parts[0] not in ("native", "gocache")):
            raise ValueError("unsupported cache archive member")
        if path.parts[0] == "native":
            rel = Path(*path.parts[1:])
            allowed = ((len(rel.parts) >= 3 and rel.parts[:2] == ("thirdparties", "install"))
                       or (len(rel.parts) >= 2 and rel.parts[0] == "lib")
                       or (len(rel.parts) == 2 and rel.parts[0] == "cgo"
                           and (rel.suffix in (".o", ".so", ".a") or rel.name == ".mo-native-provenance")))
            if not allowed:
                raise ValueError("native cache path not allowlisted")
        names.add(member.name)


def regular_files(root):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("cache symlinks not supported in this probe")
        if path.is_file():
            yield path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() != SOURCE:
        raise RuntimeError("unexpected source revision")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source):
        raise RuntimeError("tracked source modifications")
    if output.exists():
        raise RuntimeError("probe output already exists")
    if any((source / p).exists() for p in ("thirdparties/install", "cgo/libmo.so", "lib")):
        raise RuntimeError("native outputs already exist; require fresh checkout")
    for path in (source, output.parent):
        if shutil.disk_usage(path).free < 60 * GIB:
            raise RuntimeError("less than 60 GiB free; probe not started")
    output.mkdir(mode=0o700)
    os.umask(0o077)
    report = {"source": SOURCE, "scope": "native and fileservice test-binary compilation only",
              "tests_executed": False, "network_cache_transfer_measured": False, "phases": {}}
    env = dict(os.environ)
    env.update(GOMODCACHE=str(output / "gomod"), GOCACHE=str(output / "gocache"),
               GOMAXPROCS="7", GOWORK="off", GOFLAGS="", CGO_ENABLED="1")

    def save():
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")

    def measure(name, operation):
        start = time.monotonic()
        result = operation()
        report["phases"][name] = round(time.monotonic() - start, 3)
        save()
        print(name + " seconds=" + str(report["phases"][name]), flush=True)
        return result

    def command(name, argv):
        with (output / (name + ".log")).open("wb") as log:
            return measure(name, lambda: subprocess.run(argv, cwd=source, env=env,
                            stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800))

    report["toolchain"] = {}
    for name, argv in {"go": ["go", "version"], "cc": ["cc", "--version"],
                       "cxx": ["c++", "--version"], "cmake": ["cmake", "--version"]}.items():
        report["toolchain"][name] = subprocess.check_output(argv, env=env, text=True).splitlines()[0]
    report["platform"] = subprocess.check_output(["uname", "-sm"], text=True).strip()
    report["os_release_sha256"] = sha256(Path("/etc/os-release"))
    report["cpuinfo_sha256"] = sha256(Path("/proc/cpuinfo"))
    command("module_download", ["go", "mod", "download"])
    command("cold_config", ["make", "config"])
    command("cold_native", ["make", "cgo"])
    env["CGO_CFLAGS"] = f"-I{source}/cgo -I{source}/thirdparties/install/include"
    env["CGO_LDFLAGS"] = (f"-L{source}/cgo -lmo -Wl,-rpath,{source}/cgo "
                          f"-L{source}/thirdparties/install/lib -lusearch_c "
                          f"-Wl,-rpath,{source}/thirdparties/install/lib -lm")
    compile_args = ["go", "test", "-mod=readonly", "-c", "-count=1", "-short",
                    "-tags", "matrixone_test", "-p", "7", "-o", str(output / "fileservice.test"),
                    "./pkg/fileservice"]
    command("cold_go_compile", compile_args)
    cold_binary_hash = sha256(output / "fileservice.test")
    native = list(regular_files(source / "thirdparties/install")) + list(regular_files(source / "lib"))
    native += sorted(p for p in (source / "cgo").iterdir()
                     if p.is_file() and (p.suffix in (".o", ".so", ".a") or p.name == ".mo-native-provenance"))
    if any(p.is_symlink() for p in native):
        raise ValueError("native symlink not supported")
    hashes = {str(p.relative_to(source)): sha256(p) for p in native}
    cache = Path(env["GOCACHE"])
    go_files = list(regular_files(cache))
    total = sum(p.stat().st_size for p in native + go_files)
    report["native_bytes"] = sum(p.stat().st_size for p in native)
    report["gocache_bytes"] = sum(p.stat().st_size for p in go_files)
    if total > 8 * GIB or shutil.disk_usage(output).free < 3 * total + 20 * GIB:
        raise RuntimeError("cache snapshot exceeds safety budget")
    archive = output / "local-build-cache.tar.gz"

    def pack():
        with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
            for path in native:
                tar.add(path, arcname="native/" + str(path.relative_to(source)), recursive=False)
            for path in go_files:
                tar.add(path, arcname="gocache/" + str(path.relative_to(cache)), recursive=False)
    measure("cache_pack", pack)
    report["archive_bytes"] = archive.stat().st_size
    archive_hash = measure("cache_hash", lambda: sha256(archive))
    # Retain all original files in this run's own directory; never erase existing data.
    retained = output / "retained-cold"
    retained.mkdir()
    for path in native:
        target = retained / "native" / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    cache.rename(retained / "gocache")
    (output / "fileservice.test").rename(retained / "fileservice.test")

    def restore():
        if sha256(archive) != archive_hash:
            raise ValueError("archive checksum mismatch")
        with tarfile.open(archive, "r:gz") as tar:
            entries = tar.getmembers()
            members_safe(entries)
            for member in entries:
                parts = Path(member.name).parts
                target = (source if parts[0] == "native" else cache).joinpath(*parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(member.mode & 0o777)
                os.utime(target, (member.mtime, member.mtime))
        if any(sha256(source / name) != digest for name, digest in hashes.items()):
            raise ValueError("native checksum mismatch")
    measure("cache_restore_and_verify", restore)
    command("warm_config", ["make", "config"])
    # Keep the project's native provenance checks and make dependency checks enabled.
    command("warm_native", ["make", "cgo"])
    if any(sha256(source / name) != digest for name, digest in hashes.items()
           if not name.endswith(".mo-native-provenance")):
        raise RuntimeError("native content changed during make; refusing warmed Go cache")
    command("warm_go_compile", compile_args)
    report["identical_test_binary"] = sha256(output / "fileservice.test") == cold_binary_hash
    if not report["identical_test_binary"]:
        raise RuntimeError("cold/warm binary differs; cannot accept reuse")
    phases = report["phases"]
    cold = sum(phases[k] for k in ("cold_config", "cold_native", "cold_go_compile"))
    warm = sum(phases[k] for k in ("cache_restore_and_verify", "warm_config", "warm_native", "warm_go_compile"))
    preparation = phases["cache_pack"] + phases["cache_hash"]
    report.update(cold_compile_seconds=round(cold, 3), warm_restore_compile_seconds=round(warm, 3),
                  cache_preparation_seconds=round(preparation, 3),
                  saving_before_network_seconds=round(cold - warm - preparation, 3),
                  final_free_bytes=shutil.disk_usage(output).free)
    save()
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
