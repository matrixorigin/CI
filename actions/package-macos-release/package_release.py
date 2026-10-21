#!/usr/bin/env python3
"""Own the complete macOS archive; never upload a partially verified payload."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile


def run(args, timeout=60, capture=False, env=None, cwd=None):
    # gon spawns xcrun/codesign. Kill the owned group, not only gon, on timeout
    # or cancellation; otherwise notarization can outlive its staging directory.
    with subprocess.Popen(
        [str(arg) for arg in args], stdout=subprocess.PIPE if capture else None,
        text=True, env=env, cwd=cwd, start_new_session=True,
    ) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, args)
        return output or ""


def stage(workspace, package):
    package.mkdir()
    shutil.copy2(workspace / "mo-service", package / "mo-service")
    (package / "etc").mkdir()
    shutil.copytree(workspace / "etc/launch", package / "etc/launch")
    for relative in ("etc/launch-with-python-udf-server", "pkg/udf/pythonservice"):
        if (workspace / relative).is_dir():
            shutil.copytree(workspace / relative, package / relative)
    # Third-party .a files are build inputs, not runtime payloads.
    (package / "lib").mkdir()
    for library in sorted((workspace / "thirdparties/install/lib").glob("*.dylib")):
        shutil.copy2(library, package / "lib" / library.name)
    # Older releases may not dynamically link libmo; dependency validation
    # below, rather than a branch/version guess, decides whether it is required.
    if (workspace / "cgo/libmo.dylib").is_file():
        shutil.copy2(workspace / "cgo/libmo.dylib", package / "lib/libmo.dylib")
    for dictionary in (workspace / "dict", workspace / "pkg/monlp/tokenizer/dict"):
        if dictionary.is_dir():
            shutil.copytree(dictionary, package / "dict")
            break


def code_files(package):
    return sorted((package / "lib").glob("*.dylib")) + [package / "mo-service"]


def verify_dependencies(package):
    for binary in code_files(package):
        run(["lipo", binary, "-verify_arch", "arm64"])
        lines = run(["otool", "-arch", "arm64", "-L", binary], capture=True).splitlines()[1:]
        if binary.suffix == ".dylib":
            lines = lines[1:]  # LC_ID_DYLIB is this library's identity, not a dependency.
        for line in lines:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            for prefix, root in (("@rpath/", package / "lib"),
                                 ("@loader_path/", binary.parent),
                                 ("@executable_path/", package)):
                if dependency.startswith(prefix):
                    resolved = (root / dependency[len(prefix):]).resolve()
                    resolved.relative_to(package.resolve())
                    if not resolved.is_file():
                        raise ValueError(f"Missing packaged dependency: {binary.name}: {dependency}")
                    break
            else:
                raise ValueError(f"Non-portable dependency: {binary.name}: {dependency}")


def smoke(package):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DYLD_") and key not in ("LD_LIBRARY_PATH", "AC_PASSWORD", "AC_PROVIDER")}
    run([package / "mo-service", "--version"], timeout=30, env=env, cwd=package)


def package_release(workspace, tag, gon):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("Invalid release tag")
    name = f"mo-{tag}-darwin-arm64"
    destination = workspace / (name + ".zip")
    if destination.exists():
        raise FileExistsError(f"Refusing to reuse an existing release archive: {destination}")
    config = json.loads((workspace / "optools/sign/gon.json").read_text())
    # Keep the existing identity, entitlements and account configuration; never
    # serialize AC_PASSWORD/AC_PROVIDER from the environment into a file.
    with tempfile.TemporaryDirectory(prefix=".macos-release-", dir=workspace) as temp:
        scratch = Path(temp)
        package = scratch / name
        stage(workspace, package)
        verify_dependencies(package)
        sign_config = {"source": [str(p) for p in code_files(package)],
                       "bundle_id": config["bundle_id"], "sign": config["sign"],
                       "apple_id": config["apple_id"]}
        sign_file = scratch / "sign.json"
        sign_file.write_text(json.dumps(sign_config))
        run([gon, sign_file], timeout=180)
        for binary in code_files(package):
            run(["codesign", "--verify", "--strict", "--verbose=2", binary])
        smoke(package)
        archive = scratch / (name + ".zip")
        run(["ditto", "-c", "-k", "--keepParent", package, archive], timeout=180)
        notarize_file = scratch / "notarize.json"
        notarize_file.write_text(json.dumps({
            "apple_id": config["apple_id"],
            "notarize": [{"path": str(archive), "bundle_id": config["bundle_id"], "staple": False}],
        }))
        # Submit the exact final zip, not a binary-only zip followed by repacking.
        run([gon, notarize_file], timeout=1200)
        extracted = scratch / "extracted"
        run(["ditto", "-x", "-k", archive, extracted], timeout=180)
        unpacked = extracted / name
        verify_dependencies(unpacked)
        # mo-service is a standalone CLI, not an app bundle for spctl assessment.
        # Check its notarization ticket without weakening signature verification.
        run(["codesign", "--verify", "--strict", "--verbose=2",
             "-R=notarized", "--check-notarization", unpacked / "mo-service"])
        smoke(unpacked)
        # Same filesystem: failed/cancelled validation cannot expose a partial zip.
        archive.replace(destination)
    return destination


def main():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("This action requires a native Apple Silicon runner")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda number, _frame: sys.exit(128 + number))
    workspace, tag, gon = sys.argv[1:]
    archive = package_release(Path(workspace).resolve(), tag, Path(gon).resolve())
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"asset-path={archive}\n")


if __name__ == "__main__":
    main()
