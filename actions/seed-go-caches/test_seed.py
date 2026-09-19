"""Deterministic cache-import contracts; no Go, Docker, or network required."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("cache_seed", Path(__file__).with_name("seed.py"))
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)

EXISTING = "aa/" + "a" * 64 + "-a"
MISSING = "bb/" + "b" * 64 + "-d"
EXECUTABLE = "ee/" + "e" * 64 + "-d"
CONTAINER = "c" * 64


def tar_bytes(entries):
    """Entries are (name, bytes[, mode]) or explicit TarInfo objects."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                archive.addfile(entry)
            else:
                name, data, *mode = entry
                member = tarfile.TarInfo(name)
                member.size = len(data)
                if mode:
                    member.mode = mode[0]
                archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class FakeSeeder(seed.Seeder):
    """Replace external acquisition only; exercise the real importer on disk."""

    def __init__(self, root, flavor="race", generation="1"):
        super().__init__(flavor, generation)
        self.env.pop("RUNNER_ENVIRONMENT", None)
        self.root = root
        self.cache = root / "build"
        self.modules = root / "modules"
        self.docker_root = root / "docker"
        self.docker_root.mkdir(exist_ok=True)
        self.values = dict(GOCACHE=str(self.cache), GOMODCACHE=str(self.modules),
                           GOVERSION="go1.26.4", GOOS="linux", GOARCH="amd64",
                           GOAMD64="v1", GOEXPERIMENT="", GOCACHEPROG="", GOMOD=str(root / "go.mod"))
        self.image = dict(Id="sha256:" + "d" * 64, Os="linux",
                          Architecture="amd64", Size=1024)
        self.calls = []
        self.pull_failures = 0
        self.failed_source = None
        self.payloads = {
            "/mo-prebuilt/warm-status": tar_bytes([
                ("warm-status", b"warm-race=ok\nwarm-coverage=ok\n")]),
            "/root/.cache/go-build": tar_bytes([
                ("go-build/README", b"producer metadata"),
                ("go-build/trim.txt", b"producer trim"),
                ("go-build/testexpire.txt", b"producer expiration"),
                ("go-build/" + EXISTING, b"producer collision"),
                ("go-build/" + MISSING, b"imported build")]),
            "/go/pkg/mod": tar_bytes([("mod/example.test/m@v1/m.go", b"package m\n")]),
        }
        self.manifest = dict(schema=seed.SCHEMA, profile=seed.PROFILE, checkout=str(root),
                             go_env={k: self.values[k] for k in seed.GO_FIELDS},
                             flavors={"race": "ok", "coverage": "ok"}, **seed.CONTRACTS)
        self.refresh_manifest()

    def refresh_manifest(self):
        self.payloads["/mo-prebuilt/go-cache-manifest.json"] = tar_bytes([
            ("go-cache-manifest.json", json.dumps(self.manifest).encode())])

    def command(self, args, *, output=None, timeout=60):
        if args != ["go", "env", "-json", "GOCACHE", "GOMODCACHE", "GOVERSION",
                    "GOOS", "GOARCH", "GOAMD64", "GOEXPERIMENT", "GOCACHEPROG", "GOMOD"]:
            raise AssertionError(f"unexpected external command: {args}")
        # go env can initialize a cache without compiling the workload.
        for number in range(256):
            (self.cache / f"{number:02x}").mkdir(parents=True, exist_ok=True)
        for name, data in [("README", b"local metadata"), ("trim.txt", b"local trim"),
                           ("testexpire.txt", b"local expiration"),
                           (EXISTING, b"local probe")]:
            path = self.cache / name
            if not path.exists():
                path.write_bytes(data)
        return json.dumps(self.values).encode()

    def docker(self, *args, **kwargs):
        self.calls.append(args)
        config = Path(self.env["DOCKER_CONFIG"])
        if not config.is_dir() or any(config.iterdir()):
            raise AssertionError("Docker must use an empty anonymous configuration")
        if args == ("info", "--format", "{{.DockerRootDir}}"):
            return str(self.docker_root).encode()
        if args[0] == "pull":
            if self.pull_failures:
                self.pull_failures -= 1
                raise subprocess.CalledProcessError(1, ["docker", *args])
            return b""
        if args[:2] == ("image", "inspect"):
            return json.dumps([self.image]).encode()
        if args == ("create", "--name", self.container, self.image["Id"]):
            return CONTAINER.encode()
        if args[0] == "cp" and args[2:] == ("-",):
            container, source = args[1].split(":", 1)
            if container != self.container:
                raise AssertionError("copy from unowned container")
            output = kwargs["output"]
            if source == self.failed_source:
                output.write(self.payloads[source][:600])
                raise subprocess.CalledProcessError(1, ["docker", *args])
            output.write(self.payloads[source])
            return None
        raise AssertionError(f"unexpected Docker command: {args}")


class SeederTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolve macOS /var symlinks: production intentionally rejects them.
        self.root = Path(temporary.name).resolve()
        self.instances = []
        self.disk = mock.patch.object(seed.shutil, "disk_usage", return_value=
                                      SimpleNamespace(free=1024**4)).start()
        self.addCleanup(mock.patch.stopall)
        self.cleanup_calls = []

        original_cleanup = seed.Seeder.cleanup_command

        def cleanup(instance, args, timeout):
            if args[0] != "docker":
                return original_cleanup(instance, args, timeout)
            self.assertEqual(args[:3], ["docker", "rm", "-f"])
            self.assertRegex(args[3], r"^mo-go-seed-[0-9a-f]{32}$")
            self.assertTrue(Path(instance.env["DOCKER_CONFIG"]).is_dir())
            self.assertLessEqual(timeout, 15)
            self.cleanup_calls.append(args)
            return SimpleNamespace(returncode=0)

        self.external = mock.patch.object(seed.Seeder, "cleanup_command", cleanup).start()
        mock.patch.object(seed, "CHECKOUT", str(self.root)).start()
        mock.patch.object(seed, "MODULES", str(self.root / "modules")).start()

    def make(self, **kwargs):
        instance = FakeSeeder(self.root, **kwargs)
        self.instances.append(instance)
        return instance

    def run_seed(self, instance):
        prior_cleanups = len(self.cleanup_calls)
        report = instance.run()
        self.assertEqual(len(self.cleanup_calls) - prior_cleanups,
                         int(bool(instance.container)))
        self.assertFalse(list(self.root.glob(".mo-seed-*")))
        self.assertFalse(list(instance.cache.glob(".mo-seed-*")))
        self.assertFalse(list(instance.cache.glob(".seed-record-*")))
        if "DOCKER_CONFIG" in instance.env and instance.calls:
            self.assertFalse(Path(instance.env["DOCKER_CONFIG"]).exists())
        return report

    def tearDown(self):
        for instance in self.instances:
            for call in instance.calls:
                self.assertNotIn(call[0], ("prune", "rmi", "login", "run", "rm"))
                self.assertNotIn("prune", call)

    def assert_no_marker(self, instance):
        self.assertFalse((instance.cache / seed.MARKER).exists())

    def test_go_env_initialized_nonempty_cache_imports_preserving_bytes(self):
        instance = self.make()
        report = self.run_seed(instance)
        self.assertEqual(report["state"], "seeded")
        self.assertEqual((instance.cache / EXISTING).read_bytes(), b"local probe")
        self.assertEqual((instance.cache / "README").read_bytes(), b"local metadata")
        self.assertEqual((instance.cache / "trim.txt").read_bytes(), b"local trim")
        self.assertEqual((instance.cache / "testexpire.txt").read_bytes(), b"local expiration")
        self.assertEqual((instance.cache / MISSING).read_bytes(), b"imported build")
        self.assertEqual(report["imported_files"], 1)
        self.assertEqual(report["imported_bytes"], len(b"imported build"))
        self.assertEqual(json.loads((instance.cache / seed.MARKER).read_text())["key"],
                         report["key"])

    def test_second_matching_invocation_avoids_all_docker_calls(self):
        self.run_seed(self.make())
        second = self.make()
        report = self.run_seed(second)
        self.assertEqual(report["state"], "already-seeded")
        self.assertEqual(second.calls, [])
        self.assertEqual(report["previous_imported_bytes"], len(b"imported build"))
        self.assertEqual(report["imported_bytes"], 0)
        self.assertEqual(report["imported_files"], 0)
        self.assertEqual(report["module_state"], "previous-import")

    def executable_payload(self):
        directory = tarfile.TarInfo("go-build/" + EXECUTABLE)
        directory.type = tarfile.DIRTYPE
        return tar_bytes([directory, ("go-build/" + EXECUTABLE + "/tool", b"tiny executable", 0o755)])

    def test_executable_directory_is_hardlinked_with_permissions_and_no_clobber(self):
        instance = self.make()
        instance.payloads["/root/.cache/go-build"] = self.executable_payload()
        original = seed.os.link
        linked = []

        def link(source, destination):
            self.assertFalse(destination.exists())
            self.assertEqual(source.read_bytes(), b"tiny executable")
            result = original(source, destination)
            self.assertEqual(source.stat().st_ino, destination.stat().st_ino)
            linked.append(destination)
            return result

        with mock.patch.object(seed.os, "link", side_effect=link):
            report = self.run_seed(instance)
        executable = instance.cache / EXECUTABLE / "tool"
        self.assertEqual(report["state"], "seeded")
        self.assertEqual(linked, [executable])
        self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
        self.assertEqual(report["imported_files"], 1)
        self.assertEqual(report["imported_bytes"], len(b"tiny executable"))
        executable.write_bytes(b"local executable")
        retry = self.make(generation="2")
        retry.payloads["/root/.cache/go-build"] = self.executable_payload()
        with mock.patch.object(seed.os, "link", side_effect=AssertionError("must preserve existing entry")):
            report = self.run_seed(retry)
        self.assertEqual(report["state"], "seeded")
        self.assertEqual(report["imported_files"], 0)
        self.assertEqual(executable.read_bytes(), b"local executable")

    def test_executable_empty_directory_after_interruption_can_be_retried(self):
        instance = self.make()
        instance.payloads["/root/.cache/go-build"] = self.executable_payload()
        with mock.patch.object(seed.os, "link", side_effect=OSError("interrupted before link")):
            self.assertEqual(self.run_seed(instance)["state"], "failed")
        self.assert_no_marker(instance)
        self.assertEqual(list((instance.cache / EXECUTABLE).iterdir()), [])
        retry = self.make()
        retry.payloads["/root/.cache/go-build"] = self.executable_payload()
        self.assertEqual(self.run_seed(retry)["state"], "seeded")
        self.assertEqual((retry.cache / EXECUTABLE / "tool").read_bytes(), b"tiny executable")

    def test_environment_flavor_and_generation_invalidate_marker(self):
        changes = [("GOVERSION", "go1.26.5"), ("GOAMD64", "v3"),
                   ("GOEXPERIMENT", "loopvar"), ("flavor", "coverage"),
                   ("generation", "2")]
        for field, value in changes:
            with self.subTest(field=field):
                self.run_seed(self.make())
                changed = self.make(**{field: value} if field in ("flavor", "generation") else {})
                if field in changed.values:
                    changed.values[field] = value
                    changed.manifest["go_env"][field] = value
                    changed.refresh_manifest()
                report = self.run_seed(changed)
                self.assertEqual(report["state"], "seeded")
                self.assertTrue(any(call[0] == "pull" for call in changed.calls))
                self.assertEqual(report["imported_bytes"], 0)

    def test_populated_modules_are_preserved_without_extraction(self):
        instance = self.make()
        instance.modules.mkdir()
        (instance.modules / "owned").write_bytes(b"existing module")
        instance.payloads["/go/pkg/mod"] = b"deliberately invalid tar"
        report = self.run_seed(instance)
        self.assertEqual(report["state"], "seeded")
        self.assertEqual(report["module_state"], "preserved-populated")
        self.assertEqual((instance.modules / "owned").read_bytes(), b"existing module")
        self.assertFalse(any("/go/pkg/mod" in " ".join(call) for call in instance.calls))

    def test_empty_modules_publish_by_atomic_rename(self):
        instance = self.make()
        original = seed.os.rename
        observations = []

        def rename(source, destination):
            self.assertEqual(destination, instance.modules)
            self.assertTrue(destination.is_dir())
            self.assertEqual(list(destination.iterdir()), [])
            self.assertEqual((source / "example.test/m@v1/m.go").read_bytes(), b"package m\n")
            observations.append((source, destination))
            return original(source, destination)

        with mock.patch.object(seed.os, "rename", side_effect=rename):
            report = self.run_seed(instance)
        self.assertEqual(report["module_state"], "seeded")
        self.assertEqual(len(observations), 1)
        self.assertEqual((instance.modules / "example.test/m@v1/m.go").read_bytes(), b"package m\n")

    def test_module_mountpoint_skips_transfer_and_extraction(self):
        instance = self.make()
        instance.payloads["/go/pkg/mod"] = b"invalid if extracted"
        original = Path.stat

        def stat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == instance.modules:
                fields = list(result)
                fields[2] = result.st_dev + 1
                return os.stat_result(fields)
            return result

        with mock.patch.object(Path, "stat", stat):
            report = self.run_seed(instance)
        self.assertEqual(report["state"], "seeded")
        self.assertEqual(report["module_state"], "preserved-mountpoint")
        self.assertEqual(list(instance.modules.iterdir()), [])
        self.assertFalse(any("/go/pkg/mod" in " ".join(call) for call in instance.calls))

    def test_partial_transfer_never_marks_completion(self):
        for source in ("/root/.cache/go-build", "/go/pkg/mod"):
            with self.subTest(source=source):
                instance = self.make()
                instance.failed_source = source
                report = self.run_seed(instance)
                self.assertEqual(report["state"], "failed")
                self.assert_no_marker(instance)
                self.assertEqual(list(instance.modules.iterdir()), [])
                self.assertEqual((instance.cache / EXISTING).read_bytes(), b"local probe")

    def test_create_timeout_and_cancel_clean_known_owned_name(self):
        for failure in (subprocess.TimeoutExpired("docker create", 60),
                        TimeoutError("seed interrupted by signal 15")):
            with self.subTest(failure=failure):
                instance = self.make()
                original = instance.docker

                def docker(*args, **kwargs):
                    if args[0] == "create":
                        self.assertEqual(args[1:3], ("--name", instance.container))
                        self.assertRegex(instance.container, r"^mo-go-seed-[0-9a-f]{32}$")
                        raise failure
                    return original(*args, **kwargs)

                with mock.patch.object(instance, "docker", side_effect=docker):
                    self.assertEqual(self.run_seed(instance)["state"], "failed")
                self.assert_no_marker(instance)
                self.assertEqual(self.cleanup_calls[-1][-1], instance.container)

    def test_import_exception_never_marks_completion_and_retry_works(self):
        instance = self.make()
        with mock.patch.object(seed.os, "link", side_effect=OSError("injected publication failure")):
            report = self.run_seed(instance)
        self.assertEqual(report["state"], "failed")
        self.assertIn("injected publication failure", report["error"])
        self.assert_no_marker(instance)
        self.assertEqual((instance.cache / EXISTING).read_bytes(), b"local probe")
        self.assertEqual(self.run_seed(self.make())["state"], "seeded")

    def test_partial_producer_record_reused_with_accurate_health_and_counts(self):
        for health in ("FAILED",):
            with self.subTest(health=health):
                instance = self.make(generation=health)
                instance.manifest["flavors"]["race"] = health
                instance.refresh_manifest()
                first = self.run_seed(instance)
                self.assertEqual(first["state"], "seeded-partial")
                self.assertEqual(first["producer_flavor_status"], health)
                marker = json.loads((instance.cache / seed.MARKER).read_text())
                self.assertEqual(marker["state"], "seeded-partial")
                second = self.make(generation=health)
                reused = self.run_seed(second)
                self.assertEqual(reused["state"], "already-seeded")
                for field in ("producer_flavor_status", "image_id"):
                    self.assertEqual(reused[field], first[field])
                self.assertEqual(reused["previous_imported_bytes"], first["imported_bytes"])
                self.assertEqual(reused["imported_bytes"], 0)
                self.assertEqual(reused["imported_files"], 0)
                self.assertEqual(reused["module_state"], "previous-import")
                self.assertEqual(second.calls, [])

    def test_unavailable_and_registry_fallback(self):
        unavailable = self.make()
        unavailable.pull_failures = 2
        self.assertEqual(self.run_seed(unavailable)["state"], "unavailable")
        self.assert_no_marker(unavailable)
        self.assertEqual(sum(call[0] == "pull" for call in unavailable.calls), 2)
        self.assertFalse(any(call[0] == "create" for call in unavailable.calls))
        fallback = self.make()
        fallback.pull_failures = 1
        self.assertEqual(self.run_seed(fallback)["state"], "seeded")
        pulls = [call[1] for call in fallback.calls if call[0] == "pull"]
        self.assertEqual(pulls, ["registry.cn-shanghai.aliyuncs.com/matrixorigin/matrixone:ci-builder",
                                 "matrixorigin/matrixone:ci-builder"])

    def test_consumer_path_mismatch_rejects_before_any_docker_call(self):
        for field in ("GOMOD", "GOMODCACHE"):
            with self.subTest(field=field):
                instance = self.make()
                instance.values[field] = "/different/path"
                self.assertEqual(self.run_seed(instance)["state"], "incompatible-paths")
                self.assertEqual(instance.calls, [])
                self.assert_no_marker(instance)

    def test_manifest_failures_reject_before_payload_and_marker(self):
        cases = [None, [], {"schema": 1}, {"profile": "other"},
                 {"checkout": "/old/checkout"}, {"go_env": {}},
                 {"race_contract": "changed"}, {"coverage_contract": "changed"},
                 {"flavors": {"race": "unknown"}}, {"flavors": []}]
        for patch in cases:
            with self.subTest(patch=patch):
                instance = self.make()
                if isinstance(patch, dict):
                    instance.manifest.update(patch)
                    instance.refresh_manifest()
                else:
                    instance.payloads["/mo-prebuilt/go-cache-manifest.json"] = tar_bytes([
                        ("go-cache-manifest.json", json.dumps(patch).encode())])
                self.assertEqual(self.run_seed(instance)["state"], "incompatible-producer")
                self.assert_no_marker(instance)
                self.assertFalse(any("/root/.cache/go-build" in " ".join(c) or
                                     "/go/pkg/mod" in " ".join(c) for c in instance.calls))
        for payload in (b"invalid tar", tar_bytes([]),
                        tar_bytes([("go-cache-manifest.json", b"x" * 8193)]), b"x" * 65537):
            instance = self.make()
            instance.payloads["/mo-prebuilt/go-cache-manifest.json"] = payload
            self.assertEqual(self.run_seed(instance)["state"], "incompatible-producer")
            self.assert_no_marker(instance)
        instance = self.make()
        instance.failed_source = "/mo-prebuilt/go-cache-manifest.json"
        self.assertEqual(self.run_seed(instance)["state"], "incompatible-producer")
        self.assert_no_marker(instance)

    def test_old_marker_cannot_bypass_manifest_validation(self):
        instance = self.make()
        first = self.run_seed(instance)
        first["key"]["schema"] = 1
        (instance.cache / seed.MARKER).write_text(json.dumps(first))
        second = self.make()
        second.manifest["schema"] = 1
        second.refresh_manifest()
        self.assertEqual(self.run_seed(second)["state"], "incompatible-producer")
        self.assertTrue(any(c[0] == "pull" for c in second.calls))

    def test_image_platform_mismatch_never_creates_container(self):
        for field, value in (("Os", "windows"), ("Architecture", "arm64")):
            with self.subTest(field=field):
                instance = self.make()
                instance.image[field] = value
                self.assertEqual(self.run_seed(instance)["state"], "incompatible")
                self.assertFalse(any(call[0] == "create" for call in instance.calls))
                self.assert_no_marker(instance)

    def test_insufficient_build_space_stops_before_container(self):
        instance = self.make()
        # Initial floor passes, but the build archive plus extraction cannot fit.
        with mock.patch.object(seed, "FLOOR", 0), mock.patch.object(seed, "RESERVE", 100):
            self.disk.return_value.free = 2000
            self.assertEqual(self.run_seed(instance)["state"], "insufficient-space")
        self.assertTrue(any(call[0] == "pull" for call in instance.calls))
        self.assertFalse(any(call[0] == "create" for call in instance.calls))
        self.assert_no_marker(instance)

    def test_sequential_payloads_do_not_require_simultaneous_disk_budget(self):
        instance = self.make()
        # Fits one 2*1024-byte payload plus reserve, but not both at once.
        with mock.patch.object(seed, "FLOOR", 0), mock.patch.object(seed, "RESERVE", 100):
            self.disk.return_value.free = 3000
            report = self.run_seed(instance)
        self.assertEqual(report["state"], "seeded")
        self.assertEqual(report["module_state"], "seeded")

    def test_insufficient_module_headroom_keeps_completed_build_import(self):
        instance = self.make()

        def disk_usage(path):
            published = (instance.cache / MISSING).exists()
            if published:
                self.assertFalse(list(instance.cache.glob(".mo-seed-*")))
            return SimpleNamespace(free=2000 if published else 3000)

        with mock.patch.object(seed, "FLOOR", 0), mock.patch.object(seed, "RESERVE", 100):
            self.disk.side_effect = disk_usage
            report = self.run_seed(instance)
        self.assertEqual(report["state"], "seeded")
        self.assertEqual(report["module_state"], "insufficient-space")
        self.assertEqual((instance.cache / MISSING).read_bytes(), b"imported build")
        self.assertEqual(list(instance.modules.iterdir()), [])
        self.assertFalse(any("/go/pkg/mod" in " ".join(call) for call in instance.calls))
        marker = json.loads((instance.cache / seed.MARKER).read_text())
        self.assertEqual(marker["state"], "seeded")
        self.assertEqual(marker["imported_bytes"], len(b"imported build"))

    def test_inaccessible_docker_root_stops_without_pull(self):
        instance = self.make()
        original = Path.stat

        def stat(path, *args, **kwargs):
            if path == instance.docker_root:
                raise PermissionError("Docker storage is inaccessible")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            report = self.run_seed(instance)
        self.assertIn(report["state"], ("failed", "storage-unavailable"))
        self.assertEqual([call[0] for call in instance.calls], ["info"])
        self.assert_no_marker(instance)

    def test_invalid_build_archives_do_not_publish_or_mark(self):
        link = tarfile.TarInfo("go-build/" + MISSING)
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        hardlink = tarfile.TarInfo("go-build/" + MISSING)
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "go-build/" + EXISTING
        cases = {
            "malformed": b"not a tar archive",
            "empty": tar_bytes([]),
            "zero-byte-entry": tar_bytes([("go-build/" + MISSING, b"")]),
            "housekeeping-only": tar_bytes([("go-build/README", b"metadata"),
                                               ("go-build/testexpire.txt", b"expiration")]),
            "symlink": tar_bytes([link]),
            "hardlink": tar_bytes([hardlink]),
            "traversal": tar_bytes([("go-build/../../outside", b"bad")]),
            "absolute": tar_bytes([("/outside", b"bad")]),
            "wrong-root": tar_bytes([("other/" + MISSING, b"bad")]),
            "wrong-shard": tar_bytes([("go-build/aa/" + "b" * 64 + "-d", b"bad")]),
            "oversize": tar_bytes([("go-build/" + MISSING, b"x" * 1025)]),
            "aggregate-oversize": tar_bytes([("go-build/" + EXISTING, b"x" * 600),
                                               ("go-build/" + MISSING, b"y" * 600)]),
            "multiple-executables": tar_bytes([
                ("go-build/" + EXECUTABLE + "/one", b"one", 0o755),
                ("go-build/" + EXECUTABLE + "/two", b"two", 0o755)]),
            "invalid-after-valid": tar_bytes([
                ("go-build/" + MISSING, b"valid staged file"),
                ("go-build/../../outside", b"bad")]),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                instance = self.make()
                instance.payloads["/root/.cache/go-build"] = payload
                self.assertEqual(self.run_seed(instance)["state"], "failed")
                self.assert_no_marker(instance)
                self.assertFalse((instance.cache / MISSING).exists())
                self.assertFalse((instance.cache / EXECUTABLE).exists())
                self.assertEqual((instance.cache / EXISTING).read_bytes(), b"local probe")
                self.assertFalse((self.root / "outside").exists())

    def test_invalid_module_archive_preserves_empty_destination_without_marker(self):
        instance = self.make()
        instance.payloads["/go/pkg/mod"] = tar_bytes([
            ("mod/example.test/m@v1/m.go", b"valid staged module"),
            ("mod/../../outside", b"invalid")])
        report = self.run_seed(instance)
        self.assertEqual(report["state"], "failed")
        self.assert_no_marker(instance)
        self.assertEqual(list(instance.modules.iterdir()), [])
        self.assertFalse((self.root / "outside").exists())
        self.assertEqual((instance.cache / MISSING).read_bytes(), b"imported build")


class SpaceTests(unittest.TestCase):
    def test_same_filesystem_costs_are_summed_with_one_reserve(self):
        paths = [mock.Mock(), mock.Mock()]
        for path in paths:
            path.stat.return_value.st_dev = 7
        with mock.patch.object(seed, "FLOOR", 0), mock.patch.object(seed, "RESERVE", 10), \
                mock.patch.object(seed.shutil, "disk_usage") as usage:
            usage.return_value.free = 109
            self.assertFalse(seed.space_available([(paths[0], 50), (paths[1], 50)]))
            usage.assert_called_once()
            usage.return_value.free = 110
            self.assertTrue(seed.space_available([(paths[0], 50), (paths[1], 50)]))

    def test_distinct_filesystems_each_need_reserve_and_floor(self):
        paths = [mock.Mock(), mock.Mock()]
        for device, path in enumerate(paths):
            path.stat.return_value.st_dev = device
        with mock.patch.object(seed, "FLOOR", 80), mock.patch.object(seed, "RESERVE", 10), \
                mock.patch.object(seed.shutil, "disk_usage") as usage:
            free = {paths[0]: 80, paths[1]: 110}
            usage.side_effect = lambda path: SimpleNamespace(free=free[path])
            requirements = [(paths[0], 0), (paths[1], 100)]
            self.assertTrue(seed.space_available(requirements))
            free[paths[0]] = 79
            self.assertFalse(seed.space_available(requirements))
            free[paths[0]], free[paths[1]] = 80, 109
            self.assertFalse(seed.space_available(requirements))


if __name__ == "__main__":
    unittest.main()
