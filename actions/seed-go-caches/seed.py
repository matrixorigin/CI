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
import sys
import tarfile
import tempfile
import time
import uuid

SCHEMA = 2
PROFILE = "host-ut-v1"
CHECKOUT = "/home/runner/_work/matrixone/matrixone"
MODULES = "/home/runner/go/pkg/mod"
GO_FIELDS = ("GOMODCACHE", "GOVERSION", "GOOS", "GOARCH", "GOAMD64", "GOEXPERIMENT")
CONTRACTS = {
    "race_contract": "readonly-short-matrixone_test-vetoff-race-v1",
    "coverage_contract": "short-matrixone_test-vetoff-covermode-set-filter-driver-aoe-memEngine-catalog-v1",
}
FLOOR = 30 * 1024**3
RESERVE = 4 * 1024**3
ENTRY = re.compile(r"[0-9a-f]{64}-[ad]")
MARKER = ".matrixone-seed.json"


def image_candidates(pinned, hosted=False):
    """A canary may pin a digest, but may not change the trusted repository."""
    repositories = ["registry.cn-shanghai.aliyuncs.com/matrixorigin/matrixone",
                    "matrixorigin/matrixone"]
    if pinned:
        if not any(re.fullmatch(re.escape(repo) + r"@sha256:[0-9a-f]{64}", pinned)
                   for repo in repositories):
            raise ValueError("seed image must be a trusted repository at a sha256 digest")
        return [pinned]
    if hosted:
        repositories.reverse()
    return [repo + ":ci-builder" for repo in repositories]


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


def remove_owned(name, device, inode):
    """Child-only deletion, limited to a registered creation identity."""
    path = Path(name)
    if not path.is_absolute() or not path.name.startswith((".mo-seed-", "mo-docker-config-", ".seed-record-")):
        raise ValueError("not an owned temporary resource")
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or (stat.st_dev, stat.st_ino) != (int(device), int(inode)):
        raise ValueError("owned directory identity changed")
    if path.name.startswith(".seed-record-") and path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


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
        self.container_removed = False
        self.owned = {}
        self.deferred = 0
        self.cancelled = None
        self.cleanup_remaining = 45.0
        self.cleanup_each = 15.0
        self.pending_marker = None
        self.report = {"state": "failed", "flavor": flavor,
                       "module_state": "not-attempted", "imported_bytes": 0,
                       "producer_go_version": "unknown"}

    def interrupt(self, signum, _frame):
        self.cancelled = self.cancelled or f"seed interrupted by signal {signum}"
        if not self.deferred:
            raise TimeoutError(self.cancelled)

    def checkpoint(self):
        if self.cancelled:
            raise TimeoutError(self.cancelled)

    @contextlib.contextmanager
    def ownership_transition(self):
        self.deferred += 1
        try:
            yield
        finally:
            self.deferred -= 1

    def temporary(self, parent=None, prefix=".mo-seed-"):
        with self.ownership_transition():
            path = Path(tempfile.mkdtemp(dir=parent, prefix=prefix)).resolve()
            stat = path.lstat()
            self.owned[path] = (stat.st_dev, stat.st_ino)
        self.checkpoint()
        return path

    def stop_process(self, process, deadline):
        # No communicate()/wait() without a timeout, including exception paths.
        for sig, grace in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 0.7)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            until = min(deadline, time.monotonic() + grace)
            while process.poll() is None and time.monotonic() < until:
                time.sleep(min(0.02, max(0, until - time.monotonic())))
        if process.poll() is None:
            self.report.setdefault("leftover_pids", []).append(process.pid)

    def execute(self, args, *, output=None, timeout=60, cleanup=False, max_bytes=None):
        # Keep ownership across the Popen-return/try-finally gap. Cancellation
        # is checked by the polling loop, never thrown before its reaper exists.
        with self.ownership_transition():
            result = self._execute(args, output=output, timeout=timeout,
                                   cleanup=cleanup, max_bytes=max_bytes)
            if not cleanup:
                self.checkpoint()
            return result

    def _execute(self, args, *, output, timeout, cleanup, max_bytes):
        deadline = time.monotonic() + timeout
        # Disk-backed capture avoids pipe deadlocks and unbounded memory capture.
        with tempfile.TemporaryFile() as capture:
            destination = output if output is not None else capture
            with self.ownership_transition():
                process = subprocess.Popen(args, env=self.env, stdout=destination,
                                           stderr=subprocess.DEVNULL if cleanup else None,
                                           start_new_session=True)
            try:
                while process.poll() is None:
                    if not cleanup:
                        self.checkpoint()
                    if max_bytes is not None and os.fstat(destination.fileno()).st_size > max_bytes:
                        raise ValueError("metadata transfer exceeds byte limit")
                    # Reserve the last second for TERM/KILL and bounded reaping.
                    if time.monotonic() >= deadline - 1:
                        raise subprocess.TimeoutExpired(args, timeout)
                    time.sleep(0.02)
                if max_bytes is not None and os.fstat(destination.fileno()).st_size > max_bytes:
                    raise ValueError("metadata transfer exceeds byte limit")
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, args)
                if output is None:
                    capture.seek(0)
                    return capture.read()
            finally:
                if process.poll() is None:
                    with self.ownership_transition():
                        self.stop_process(process, min(deadline, time.monotonic() + 1))

    def cleanup_command(self, args, timeout):
        return self.execute(args, timeout=timeout, cleanup=True)

    def cleanup_path(self, path):
        if path not in self.owned:
            return True
        started = time.monotonic()
        allowance = min(self.cleanup_each, self.cleanup_remaining)
        if allowance <= 1:
            return False
        with self.ownership_transition():
            try:
                device, inode = self.owned[path]
                self.cleanup_command([sys.executable, str(Path(__file__).resolve()),
                                      "--remove-owned", str(path), str(device), str(inode)], allowance)
                if path.exists() or path.is_symlink():
                    raise OSError("cleanup did not remove owned directory")
                del self.owned[path]
                return True
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                self.report.setdefault("cleanup_errors", []).append(f"{path}: {error}")
                return False
            finally:
                self.cleanup_remaining -= time.monotonic() - started

    def clean_container(self):
        if not self.container or self.container_removed:
            return
        started = time.monotonic()
        allowance = min(self.cleanup_each, self.cleanup_remaining)
        if allowance <= 1:
            return
        with self.ownership_transition():
            try:
                self.cleanup_command(["docker", "rm", "-f", self.container], allowance)
                self.container_removed = True
            except (OSError, subprocess.SubprocessError) as error:
                self.report.setdefault("cleanup_errors", []).append(f"{self.container}: {error}")
            finally:
                self.cleanup_remaining -= time.monotonic() - started

    def command(self, args, *, output=None, timeout=60, max_bytes=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("seed deadline expired")
        self.checkpoint()
        return self.execute(args, output=output, timeout=min(timeout, remaining), max_bytes=max_bytes)

    def docker(self, *args, **kwargs):
        return self.command(["docker", *args], **kwargs)

    def payload(self, cache, source, root, budget, build=False):
        stage = self.temporary(cache if build else cache.parent)
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
        pinned = self.env.get("SEED_IMAGE", "")
        images = image_candidates(pinned, self.env.get("RUNNER_ENVIRONMENT") == "github-hosted")
        values = json.loads(self.command([
            "go", "env", "-json", "GOCACHE", "GOMODCACHE", "GOVERSION",
            "GOOS", "GOARCH", "GOAMD64", "GOEXPERIMENT", "GOCACHEPROG", "GOMOD"]))
        if (values["GOCACHE"] == "off" or values.get("GOCACHEPROG")
                or values["GOOS"] != "linux" or values["GOARCH"] != "amd64"):
            self.report["state"] = "unsupported"
            return
        if values.get("GOMOD") != CHECKOUT + "/go.mod" or values["GOMODCACHE"] != MODULES:
            self.report["state"] = "incompatible-paths"
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
               "consumer": values, "flavor": self.flavor,
               "profile": PROFILE, "contracts": CONTRACTS, "checkout": CHECKOUT}
        if pinned:
            key["image"] = pinned
        marker = cache / MARKER
        if marker.is_symlink():
            raise ValueError("symlink completion record")
        try:
            prior = json.loads(marker.read_text())
        except (FileNotFoundError, ValueError):
            prior = {}
        if not isinstance(prior, dict):
            prior = {}
        if prior.get("key") == key and prior.get("state") in ("seeded", "seeded-partial"):
            self.report.update(prior)
            self.report.update(state="already-seeded", previous_imported_bytes=prior.get("imported_bytes", 0),
                               imported_bytes=0, imported_files=0, module_state="previous-import",
                               acquisition_seconds=0, build_import_seconds=0, module_import_seconds=0)
            return
        # An empty config prevents reuse of runner registry credentials.
        config = self.temporary(prefix="mo-docker-config-")
        self.env["DOCKER_CONFIG"] = str(config)
        docker_root = Path(self.docker("info", "--format", "{{.DockerRootDir}}").decode().strip())
        if not docker_root.is_absolute() or not docker_root.is_dir():
            self.report["state"] = "storage-unavailable"
            return
        if not space_available([(docker_root, 0), (cache, 0), (modules, 0)]):
            self.report["state"] = "insufficient-space"
            return
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
        try:
            # Reject legacy/incompatible producers BEFORE large cache payloads.
            with tempfile.TemporaryFile() as metadata:
                self.docker("cp", f"{self.container}:/mo-prebuilt/go-cache-manifest.json", "-",
                            output=metadata, timeout=15, max_bytes=65536)
                if metadata.tell() > 65536:
                    raise ValueError("oversize metadata archive")
                metadata.seek(0)
                with tarfile.open(fileobj=metadata, mode="r:") as archive:
                    member = archive.getmember("go-cache-manifest.json")
                    if not member.isfile() or member.size > 8192:
                        raise ValueError("invalid manifest member")
                    manifest = json.load(archive.extractfile(member))
            expected = {"schema": SCHEMA, "profile": PROFILE, "checkout": CHECKOUT,
                        "go_env": {field: values[field] for field in GO_FIELDS}, **CONTRACTS}
            if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in expected.items()):
                raise ValueError("producer manifest does not match host UT contract")
            status = manifest.get("flavors", {}).get(self.flavor)
            if status not in ("ok", "FAILED"):
                raise ValueError("missing producer flavor status")
        except (subprocess.SubprocessError, tarfile.TarError, KeyError, UnicodeError,
                ValueError, AttributeError, TypeError) as error:
            self.report.update(state="incompatible-producer", error=str(error))
            return
        self.report["producer_go_version"] = manifest["go_env"]["GOVERSION"]
        self.report["producer_flavor_status"] = status
        self.report["acquisition_seconds"] = round(time.monotonic() - self.started, 3)
        phase_started = time.monotonic()
        data = self.payload(cache, "/root/.cache/go-build", "go-build", size, True)
        count, imported = publish_build(data, cache)
        self.report.update(imported_files=count, imported_bytes=imported)
        if not self.cleanup_path(data.parent):
            raise OSError("build staging cleanup failed; module import not started")
        self.checkpoint()
        self.report["build_import_seconds"] = round(time.monotonic() - phase_started, 3)
        self.report["module_state"] = "preserved-mountpoint" if module_mount else "preserved-populated"
        # Recheck after releasing build staging. Do not budget two archives
        # simultaneously or allocate the module payload only to discard it.
        if need_modules and not space_available([(modules, size * 2), (cache, 0), (docker_root, 0)]):
            self.report["module_state"] = "insufficient-space"
            need_modules = False
        if need_modules:
            phase_started = time.monotonic()
            data = self.payload(modules, "/go/pkg/mod", "mod", size)
            with self.ownership_transition():
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
            self.checkpoint()
            if not self.cleanup_path(data.parent):
                raise OSError("module staging cleanup failed")
            self.checkpoint()
            self.report["module_import_seconds"] = round(time.monotonic() - phase_started, 3)
        self.report.update(key=key, state="seeded" if status == "ok" else "seeded-partial",
                           completed_at=int(time.time()))
        self.pending_marker = cache

    def commit_marker(self):
        # File creation, publication and cleanup form one short ownership
        # transition. Signals are remembered; a cancelled commit is removed.
        cache = self.pending_marker
        self.checkpoint()
        with self.ownership_transition():
            fd, name = tempfile.mkstemp(prefix=".seed-record-", dir=cache)
            path = Path(name)
            stat = os.fstat(fd)
            self.owned[path] = (stat.st_dev, stat.st_ino)
            published = False
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(self.report, output, sort_keys=True)
                if not self.cancelled:
                    os.replace(name, cache / MARKER)
                    published = True
                    del self.owned[path]
                # This is the final cancellation checkpoint for the commit.
                # It must be inside the rollback scope: detecting cancellation
                # outside finally could leave a seeded marker on a failed run.
                self.checkpoint()
            except BaseException:
                # Cancellation detected at/before the final checkpoint aborts
                # publication. Signals after it belong to a completed commit:
                # do not turn seeded into a marker-less apparent success.
                if published:
                    (cache / MARKER).unlink(missing_ok=True)
                raise
            finally:
                if path in self.owned:
                    self.cleanup_path(path)

    def run(self):
        cleanup_budget = self.cleanup_remaining
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
        for sig in handlers:
            signal.signal(sig, self.interrupt)
        with contextlib.ExitStack() as stack:
            try:
                signal.alarm(1080)
                self.seed(stack)
            except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError,
                    TimeoutError) as error:
                self.report.update(state="failed", error=str(error))
            finally:
                # Cover the entire finalization interval, including gaps between
                # cleanup calls and marker publication, against repeated signals.
                self.deferred += 1
                with self.ownership_transition():
                    signal.alarm(0)
                    # Last container-removal attempt precedes config release.
                    self.clean_container()
                    for path in list(self.owned):
                        self.cleanup_path(path)
                    leftovers = [str(path) for path in self.owned]
                    if self.container and not self.container_removed:
                        leftovers.append("container:" + self.container)
                    self.report["cleanup"] = "incomplete" if leftovers or self.report.get("leftover_pids") else "complete"
                    if leftovers:
                        self.report["leftover_resources"] = leftovers
                    if self.cancelled:
                        self.report.update(state="cancelled", error=self.cancelled)
                    elif self.report["cleanup"] != "complete":
                        self.report.update(state="failed", error="owned resource cleanup incomplete")
                if self.pending_marker and self.report["state"] in ("seeded", "seeded-partial"):
                    try:
                        self.commit_marker()
                    except (OSError, TimeoutError) as error:
                        self.report.update(state="cancelled" if self.cancelled else "failed", error=str(error))
                if self.owned:
                    self.report.update(state="cancelled" if self.cancelled else "failed", cleanup="incomplete",
                                       leftover_resources=[str(path) for path in self.owned] +
                                       (["container:" + self.container] if self.container and not self.container_removed else []))
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                self.deferred -= 1
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.report["cleanup_seconds"] = round(cleanup_budget - self.cleanup_remaining, 3)
        return self.report


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--remove-owned":
        remove_owned(*sys.argv[2:])
        sys.exit(0)
    result = Seeder(os.environ["SEED_FLAVOR"], os.environ.get("SEED_GENERATION", "1")).run()
    print(json.dumps(result, sort_keys=True))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("### Go cache import (not measured cache hits)\n\n```json\n"
                          + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")
