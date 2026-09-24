import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


EXPECTED_SHA = "a" * 40
MOVED_SHA = "b" * 40
VALID_DIFF = b"diff --git a/main.go b/main.go\n--- a/main.go\n+++ b/main.go\n@@ -1 +1 @@\n-old\n+new\n"


FAKE_CURL = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith("https://"))
output = Path(args[args.index("--output") + 1])
has_auth = any(
    args[i + 1] == "Authorization: Bearer " + os.environ["GH_TOKEN"]
    for i, arg in enumerate(args[:-1])
    if arg == "-H"
)
mode = os.environ["FAKE_CURL_MODE"]
state_path = Path(os.environ["FAKE_CURL_STATE"])
try:
    state = json.loads(state_path.read_text())
except FileNotFoundError:
    state = {"heads": 0, "calls": []}

api_diff = any("application/vnd.github.diff" in arg for arg in args)
if "/pulls/42" in url and "api.github.com" in url and not api_diff:
    state["heads"] += 1
    head_sha = (
        os.environ["MOVED_SHA"]
        if mode == "head-moves" and state["heads"] > 1
        else os.environ["EXPECTED_SHA"]
    )
    changed_files = 0 if mode in ("public-empty", "public-nonempty-metadata-empty") else 1
    if mode == "changed-files-move" and state["heads"] > 1:
        changed_files = 2
    if mode == "metadata-two-files":
        changed_files = 2
    body = json.dumps({"head": {"sha": head_sha}, "changed_files": changed_files}).encode()
    status = 200
    kind = "metadata"
elif "github.com/owner/repo/pull/42.diff" in url:
    kind = "public"
    if mode in ("public-503-api-ok", "api-fails"):
        status, body = 503, b"partial service error"
    elif mode in ("public-empty", "public-empty-api-ok", "both-empty"):
        status, body = 200, b""
    elif mode == "public-html":
        status, body = 200, b"<html>temporary failure</html>"
    elif mode == "public-truncated":
        status, body = 200, b"diff --git a/main.go b/main.go\n"
    elif mode in ("public-ok", "head-moves", "changed-files-move", "public-nonempty-metadata-empty", "metadata-two-files"):
        status, body = 200, os.environ["VALID_DIFF"].encode()
    else:
        status, body = 500, b"unexpected mode"
elif "api.github.com/repos/owner/repo/pulls/42" in url and api_diff:
    kind = "api-diff"
    if mode == "api-fails":
        status, body = 403, b"forbidden"
    elif mode == "both-empty":
        status, body = 200, b""
    else:
        status, body = 200, os.environ["VALID_DIFF"].encode()
else:
    kind = "unexpected"
    status, body = 500, b"unexpected URL"

state["calls"].append({
    "kind": kind,
    "authenticated": has_auth,
    "retry_count": args[args.index("--retry") + 1],
    "has_timeout": "--max-time" in args,
})
state_path.write_text(json.dumps(state))
output.write_bytes(body)
sys.stdout.write(str(status))
if status >= 400:
    sys.exit(22)
'''


class FetchPublicPRDiffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(FAKE_CURL)
        fake_curl.chmod(0o755)
        self.state_path = self.root / "curl-state.json"
        self.output = self.root / "matrixone" / "diff.patch"
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{bin_dir}:{self.env['PATH']}",
                "GH_TOKEN": "test-token-must-not-be-logged",
                "FAKE_CURL_STATE": str(self.state_path),
                "EXPECTED_SHA": EXPECTED_SHA,
                "MOVED_SHA": MOVED_SHA,
                "VALID_DIFF": VALID_DIFF.decode(),
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_fetch(self, mode):
        self.env["FAKE_CURL_MODE"] = mode
        return subprocess.run(
            [
                "bash",
                str(Path(__file__).with_name("fetch_public_pr_diff.sh")),
                "owner/repo",
                "42",
                EXPECTED_SHA,
                str(self.output),
            ],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def test_public_503_falls_back_to_api_and_checks_head_twice(self):
        result = self.run_fetch("public-503-api-ok")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), VALID_DIFF)
        state = self.read_state()
        self.assertEqual(state["heads"], 2)
        self.assertEqual([call["kind"] for call in state["calls"]], [
            "metadata", "public", "api-diff", "metadata"
        ])
        self.assertFalse(state["calls"][1]["authenticated"])
        self.assertTrue(state["calls"][2]["authenticated"])
        self.assertNotIn(self.env["GH_TOKEN"], result.stdout + result.stderr)
        self.assertEqual(state["calls"][1]["retry_count"], "4")
        self.assertTrue(all(call["has_timeout"] for call in state["calls"]))

    def test_api_failure_does_not_publish_partial_diff(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b"stale diff from an earlier attempt")
        result = self.run_fetch("api-fails")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertIn("HTTP 403", result.stderr)
        self.assertEqual(list(self.output.parent.glob(".fetch-pr-diff.*")), [])

    def test_head_change_during_download_does_not_publish_diff(self):
        result = self.run_fetch("head-moves")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertIn("PR head moved after diff download", result.stderr)

    def test_empty_diff_for_zero_changed_files_is_preserved(self):
        result = self.run_fetch("public-empty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.exists())
        self.assertEqual(self.output.read_bytes(), b"")
        self.assertEqual([call["kind"] for call in self.read_state()["calls"]], [
            "metadata", "public", "metadata"
        ])

    def test_empty_public_diff_with_changed_files_falls_back_to_api(self):
        result = self.run_fetch("public-empty-api-ok")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), VALID_DIFF)
        self.assertIn("empty diff despite changed_files=1", result.stderr)
        self.assertEqual([call["kind"] for call in self.read_state()["calls"]], [
            "metadata", "public", "api-diff", "metadata"
        ])

    def test_empty_diff_from_both_sources_does_not_pass_coverage(self):
        result = self.run_fetch("both-empty")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertIn("Unable to download a valid PR diff", result.stderr)

    def test_nonempty_diff_with_zero_changed_files_is_rejected(self):
        result = self.run_fetch("public-nonempty-metadata-empty")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertIn("non-empty diff despite changed_files=0", result.stderr)

    def test_changed_file_count_moving_during_download_does_not_publish_diff(self):
        result = self.run_fetch("changed-files-move")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertIn("PR changed-file count moved during diff download", result.stderr)

    def test_non_diff_http_200_body_falls_back_instead_of_becoming_no_changes(self):
        result = self.run_fetch("public-html")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), VALID_DIFF)
        self.assertIn("not a Git diff", result.stderr)
        self.assertIn("api-diff", [call["kind"] for call in self.read_state()["calls"]])

    def test_truncated_http_200_diff_falls_back_to_api(self):
        result = self.run_fetch("public-truncated")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), VALID_DIFF)
        self.assertIn("malformed Git diff", result.stderr)
        self.assertIn("api-diff", [call["kind"] for call in self.read_state()["calls"]])

    def test_diff_with_fewer_files_than_metadata_is_rejected(self):
        result = self.run_fetch("metadata-two-files")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertIn("diff has file_count=1 despite changed_files=2", result.stderr)


if __name__ == "__main__":
    unittest.main()
