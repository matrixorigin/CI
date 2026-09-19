#!/usr/bin/env python3
"""Best-effort import, not a compiler-cache hit oracle. Linux runners only."""

import contextlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid

SCHEMA = 1
FLOOR = 30 * 1024**3
RESERVE = 4 * 1024**3
ENTRY = re.compile(r"[0-9a-f]{64}-[ad]")
MARKER = ".matrixone-seed.json"


def directory(value):
    path = Path(value)
    if not path.is_absolute() or path == Path("/"):
        raise ValueError("cache path must be an absolute non-root directory")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"symlink cache path: {part}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def space_available(requirements):
    """Sum simultaneous costs on the same filesystem, reserve once per device."""
    devices = {}
    for path, size in requirements:
        dev = path.stat().st_dev
        previous = devices.get(dev, (path, 0))
        devices[dev] = (path, previous[1] + size)
    return all(shutil.disk_usage(path).free >= max(FLOOR, size + RESERVE)
               for path, size in devices.values())


def extract(archive, destination, root, budget, build=False):
    """No tar extraction APIs: allow only directories/regular files under root."""
    total = 0
    with tarfile.open(archive, "r|*") as stream:
        for member in stream:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise ValueError("unsafe archive path")
            if name.parts[0] != root:
                raise ValueError("unexpected archive root")
            relative = name.parts[1:]
            if not (member.isdir() or member.isfile()):
                raise ValueError("archive links/special files are not allowed")
            if not relative:
                if not member.isdir():
                    raise ValueError("archive root is not a directory")
                continue
            if build:
                shard_ok = bool(re.fullmatch(r"[0-9a-f]{2}", relative[0]))
                entry_ok = (len(relative) >= 2 and ENTRY.fullmatch(relative[1])
                            and relative[0] == relative[1][:2])
                if member.isdir():
                    if not ((len(relative) == 1 and shard_ok) or
                            (len(relative) == 2 and entry_ok and relative[1].endswith("-d"))):
                        raise ValueError("unexpected build cache directory")
                elif relative in (("README",), ("trim.txt",), ("testexpire.txt",)):
                    continue  # Local Go owns cache housekeeping metadata.
                elif not (entry_ok and (len(relative) == 2 or
                          (len(relative) == 3 and relative[1].endswith("-d")))):
                    raise ValueError("unexpected build cache entry")
            target = destination.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            if total > budget:
                raise ValueError("cache payload exceeds extraction budget")
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if build:
        for shard in destination.iterdir():
            for entry in shard.iterdir():
                if entry.is_dir() and len(list(entry.iterdir())) != 1:
                    raise ValueError("executable cache entry must contain one file")
    return total


def publish_build(stage, cache):
    count = size = 0
    for shard in stage.iterdir():
        target = cache / shard.name
        if target.is_symlink():
            raise ValueError("symlink destination shard")
        target.mkdir(exist_ok=True)
        for entry in shard.iterdir():
            final = target / entry.name
            if entry.is_dir():
                # Go 1.24+ keeps a cached executable inside <hash>-d/name.
                # An empty directory is a cache miss; the only file is still
                # published atomically. Preserve any already-populated entry.
                if final.is_symlink():
                    raise ValueError("symlink executable cache entry")
                final.mkdir(exist_ok=True)
                if any(final.iterdir()):
                    continue
                entry = next(entry.iterdir())
                final = final / entry.name
            try:
                # Same-filesystem hardlink publishes a whole file atomically,
                # with no overwrite and no second payload copy.
                os.link(entry, final)
            except FileExistsError:
                if final.is_symlink() or not final.is_file():
                    raise ValueError("unsafe existing cache entry")
            else:
                count += 1
                size += entry.stat().st_size
    return count, size


def write_marker(cache, record):
    fd, name = tempfile.mkstemp(prefix=".seed-record-", dir=cache)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, sort_keys=True)
        os.replace(name, cache / MARKER)
    finally:
        Path(name).unlink(missing_ok=True)


class Seeder:
    def __init__(self, flavor, generation):
        if flavor not in ("race", "coverage"):
            raise ValueError("unsupported flavor")
        self.flavor = flavor
        self.generation = generation
        self.started = time.monotonic()
        self.deadline = self.started + 1080
        self.env = os.environ.copy()
        self.container = ""
        self.report = {"state": "failed", "flavor": flavor,
                       "module_state": "not-attempted", "imported_bytes": 0,
                       "producer_go_version": "unknown"}

    def command(self, args, *, output=None, timeout=60):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("seed deadline expired")
        return subprocess.run(args, check=True, env=self.env,
                              stdout=output if output is not None else subprocess.PIPE,
                              timeout=min(timeout, remaining)).stdout

    def docker(self, *args, **kwargs):
        return self.command(["docker", *args], **kwargs)

    def payload(self, stack, cache, source, root, budget, build=False):
        stage = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix=".mo-seed-", dir=cache if build else cache.parent)))
        archive = stage / "payload.tar"
        data = stage / "data"
        data.mkdir()
        with archive.open("wb") as output:
            self.docker("cp", f"{self.container}:{source}", "-",
                        output=output, timeout=300)
        extracted = extract(archive, data, root, budget, build)
        if build and extracted == 0:
            raise ValueError("empty build cache payload")
        archive.unlink()
        return data

    def seed(self, stack):
        values = json.loads(self.command([
            "go", "env", "-json", "GOCACHE", "GOMODCACHE", "GOVERSION",
            "GOOS", "GOARCH", "GOAMD64", "GOEXPERIMENT", "GOCACHEPROG"]))
        if (values["GOCACHE"] == "off" or values.get("GOCACHEPROG")
                or values["GOOS"] != "linux" or values["GOARCH"] != "amd64"):
            self.report["state"] = "unsupported"
            return
        cache = directory(values["GOCACHE"])
        modules = directory(values["GOMODCACHE"])
        if cache == modules or cache in modules.parents or modules in cache.parents:
            raise ValueError("overlapping cache paths")
        lock_path = cache / ".matrixone-seed.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = stack.enter_context(os.fdopen(lock_fd, "w"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.report["state"] = "busy"
            return
        key = {"schema": SCHEMA, "generation": self.generation,
               "consumer": values, "flavor": self.flavor}
        marker = cache / MARKER
        if marker.is_symlink():
            raise ValueError("symlink completion record")
        try:
            prior = json.loads(marker.read_text())
        except (FileNotFoundError, ValueError):
            prior = {}
        if prior.get("key") == key and prior.get("state") in ("seeded", "seeded-partial"):
            self.report.update(prior)
            self.report.update(state="already-seeded", previous_imported_bytes=prior.get("imported_bytes", 0),
                               imported_bytes=0, imported_files=0, module_state="previous-import")
            return
        # An empty config prevents reuse of runner registry credentials.
        config = stack.enter_context(tempfile.TemporaryDirectory(prefix="mo-docker-config-"))
        self.env["DOCKER_CONFIG"] = config
        docker_root = Path(self.docker("info", "--format", "{{.DockerRootDir}}").decode().strip())
        if not docker_root.is_absolute() or not docker_root.is_dir():
            self.report["state"] = "storage-unavailable"
            return
        if not space_available([(docker_root, 0), (cache, 0), (modules, 0)]):
            self.report["state"] = "insufficient-space"
            return
        images = ["registry.cn-shanghai.aliyuncs.com/matrixorigin/matrixone:ci-builder",
                  "matrixorigin/matrixone:ci-builder"]
        if self.env.get("RUNNER_ENVIRONMENT") == "github-hosted":
            images.reverse()
        image = None
        for candidate in images:
            try:
                self.docker("pull", candidate, timeout=300)
                image = json.loads(self.docker("image", "inspect", candidate))[0]
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
        if image is None:
            self.report["state"] = "unavailable"
            return
        self.report.update(image_id=image["Id"], image_os=image["Os"],
                           image_arch=image["Architecture"])
        if image["Os"] != values["GOOS"] or image["Architecture"] != values["GOARCH"]:
            self.report["state"] = "incompatible"
            return
        size = int(image["Size"])
        module_mount = modules.stat().st_dev != modules.parent.stat().st_dev
        need_modules = not module_mount and not any(modules.iterdir())
        # Archive plus extracted payload, on actual backing filesystems.
        # This is a conservative preflight, not a quota against other writers.
        if size <= 0 or not space_available([
            (cache, size * 2), (modules, 0), (docker_root, 0)]):
            self.report["state"] = "insufficient-space"
            return
        # Establish an unpredictable owned name BEFORE creating the resource:
        # timeout/cancellation may lose stdout after the daemon created it.
        self.container = "mo-go-seed-" + uuid.uuid4().hex
        identity = self.docker("create", "--name", self.container, image["Id"]).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", identity):
            raise ValueError("unexpected container identity")
        status = "unknown"
        try:
            # A tiny metadata archive only; never execute image commands.
            with tempfile.TemporaryFile() as metadata:
                self.docker("cp", f"{self.container}:/mo-prebuilt/warm-status", "-", output=metadata)
                metadata.seek(0)
                with tarfile.open(fileobj=metadata) as archive:
                    member = archive.getmember("warm-status")
                    if member.isfile() and member.size <= 4096:
                        lines = archive.extractfile(member).read().decode().splitlines()
                        if f"warm-{self.flavor}=ok" in lines:
                            status = "ok"
                        elif f"warm-{self.flavor}=FAILED" in lines:
                            status = "FAILED"
        except (subprocess.CalledProcessError, tarfile.TarError, KeyError, UnicodeError):
            pass
        self.report["producer_flavor_status"] = status
        with contextlib.ExitStack() as build_stack:
            data = self.payload(build_stack, cache, "/root/.cache/go-build", "go-build", size, True)
            count, imported = publish_build(data, cache)
            self.report.update(imported_files=count, imported_bytes=imported)
        self.report["module_state"] = "preserved-mountpoint" if module_mount else "preserved-populated"
        # Recheck after releasing build staging. Do not budget two archives
        # simultaneously or allocate the module payload only to discard it.
        if need_modules and not space_available([(modules, size * 2), (cache, 0), (docker_root, 0)]):
            self.report["module_state"] = "insufficient-space"
            need_modules = False
        if need_modules:
            with contextlib.ExitStack() as module_stack:
                data = self.payload(module_stack, modules, "/go/pkg/mod", "mod", size)
                try:
                    # POSIX atomically replaces an empty directory; a populated
                    # destination fails without removing it. Never rmdir first.
                    os.rename(data, modules)
                except OSError:
                    if not any(modules.iterdir()):
                        raise
                    self.report["module_state"] = "preserved-populated"
                else:
                    self.report["module_state"] = "seeded"
        self.report.update(key=key, state="seeded" if status == "ok" else "seeded-partial",
                           completed_at=int(time.time()))
        write_marker(cache, self.report)

    def run(self):
        with contextlib.ExitStack() as stack:
            try:
                self.seed(stack)
            except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError,
                    TimeoutError) as error:
                self.report.update(state="failed", error=str(error))
            finally:
                if self.container:
                    # Keep the anonymous client config alive through cleanup.
                    # Cleanup has a short budget even after the work deadline.
                    try:
                        subprocess.run(["docker", "rm", "-f", self.container],
                                       env=self.env, check=True, timeout=15,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except (OSError, subprocess.SubprocessError):
                        self.report["cleanup"] = "container-removal-failed"
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        return self.report


def interrupted(signum, _frame):
    raise TimeoutError(f"seed interrupted by signal {signum}")


if __name__ == "__main__":
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        signal.signal(sig, interrupted)
    signal.alarm(1100)
    result = Seeder(os.environ["SEED_FLAVOR"], os.environ.get("SEED_GENERATION", "1")).run()
    signal.alarm(0)
    print(json.dumps(result, sort_keys=True))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("### Go cache import (not measured cache hits)\n\n```json\n"
                          + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")
