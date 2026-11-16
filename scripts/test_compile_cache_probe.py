import tarfile
import unittest
from compile_cache_probe import members_safe


class ArchivePolicyTests(unittest.TestCase):
    def test_allowlisted_files(self):
        members_safe([tarfile.TarInfo(n) for n in (
            "native/cgo/libmo.so", "native/cgo/.mo-native-provenance",
            "native/thirdparties/install/include/header.h", "gocache/ab/123-d")])

    def test_paths_rejected(self):
        for path in ("/etc/file", "native/../../file", "native/go.mod", "native/cgo/Makefile", "other/file"):
            with self.assertRaises(ValueError):
                members_safe([tarfile.TarInfo(path)])

    def test_links_and_duplicates_rejected(self):
        member = tarfile.TarInfo("native/cgo/libmo.so")
        member.type = tarfile.SYMTYPE
        with self.assertRaises(ValueError): members_safe([member])
        with self.assertRaises(ValueError):
            members_safe([tarfile.TarInfo("gocache/file"), tarfile.TarInfo("gocache/file")])


if __name__ == "__main__":
    unittest.main()
