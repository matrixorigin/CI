"""Trusted event adapter. No dispatch, cache publication or PR-head execution."""
import datetime as dt
from http.client import HTTPException
import json
import os
import re
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROJECT = "matrixorigin/matrixone"
ENTRY = PROJECT + "/.github/workflows/entrypoint.yaml@refs/heads/main"
# Keep 4.2-dev on legacy until admission can verify the S3 fixtures at the PR head.
CACHE_BASE_REFS = frozenset({"main"})
# Reviewed fixture blobs from MatrixOne #32590 and the current main variant
# of s3_fs_test.go. Unknown variants stay on legacy until reviewed.
S3_FIXTURE_BLOBS = {
    "cs_s3_fixture_test.go": {"250da6016644c44c6d8750b160e7391f5944fe02"},
    "object_storage_arguments_test.go": {"24c979e693c1456ef9d82e6eb96da7f5de355347"},
    "object_storage_test.go": {"27664e29685578d5947e466dcbb6a882d1327ea9"},
    "s3_fs_test.go": {"99e213a8b103ac9e06d583d81ebcc9000f3006bf",
                      "dad119552630af938c2b964edda003c5e7558bbb"},
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def has_s3_fixture(source, sha, token):
    request = Request(f"https://api.github.com/repos/{source}/contents/pkg/fileservice?ref={sha}",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with build_opener(NoRedirect).open(request, timeout=10) as response:
            limit = 512 * 1024
            size = response.getheader("Content-Length")
            raw = response.read(limit + 1)
            # A bounded read can return early without raising IncompleteRead.
            if len(raw) > limit or (size is not None and len(raw) != int(size)):
                return False
            entries = json.loads(raw)
    except (OSError, HTTPException, ValueError):
        return False
    if not isinstance(entries, list):
        return False
    blobs = {item.get("name"): item.get("sha") for item in entries
             if isinstance(item, dict) and item.get("type") == "file"
             and isinstance(item.get("name"), str) and isinstance(item.get("sha"), str)}
    return all(blobs.get(name) in approved for name, approved in S3_FIXTURE_BLOBS.items())


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def route(ctx, event, live, member_status, verify_s3_fixture):
    require(ctx == (PROJECT, "pull_request_target", ENTRY), "unexpected caller")
    require(event["action"] in {"opened", "synchronize", "reopened", "ready_for_review"},
            "unexpected event action")
    pr = event["pull_request"]
    number = event["number"]
    require(type(number) is int and 0 < number < 10_000_000
            and number == pr["number"] == live["number"], "PR number differs")
    def identity(p):
        return (p["base"]["repo"]["full_name"], p["base"]["ref"], p["head"]["repo"]["full_name"],
                p["head"]["sha"], p["user"]["login"])
    project, base, source, sha, author = identity(live)
    require(identity(pr) == identity(live) and project == PROJECT
            and live["state"] == "open" and re.fullmatch(r"[0-9a-f]{40}", sha)
            and re.fullmatch(r"[\w.-]+/[\w.-]+", source, re.ASCII)
            and re.fullmatch(r"[\w-]+", author, re.ASCII), "stale or invalid PR identity")
    # The approved client never follows membership redirects. A 302 is not
    # proof of membership; like a 404 it must never grant access to cache runners.
    require(member_status in (204, 302, 404), "membership could not be verified")
    # Never use an arbitrary caller-selected head, runner label or environment.
    # Draft and PR type labels do not alter CS eligibility. The old path
    # still enforces its own org/safe-to-test eligibility.
    if base not in CACHE_BASE_REFS or live["head"]["repo"]["archived"] or member_status != 204:
        return {"route": "legacy", "allowed": "false"}
    if not verify_s3_fixture(source, sha):
        return {"route": "legacy", "allowed": "false"}
    return {"route": "cache", "allowed": "true", "head_sha": sha, "head_repository": source}


def main():
    import node_cache_real_pr_gate as approved  # immutable reviewed helper checkout
    ctx = tuple(os.environ[k] for k in ("GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_WORKFLOW_REF"))
    require(ctx == (PROJECT, "pull_request_target", ENTRY), "unexpected caller")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    number = event["number"]
    require(type(number) is int and 0 < number < 10_000_000, "invalid PR number")
    token = os.environ["CS_PR_METADATA_TOKEN"]
    status, pr = approved.read(f"repos/{PROJECT}/pulls/{number}", token)
    require(status == 200, "PR metadata unavailable")
    status, checks = approved.membership(pr["user"]["login"], os.environ["CS_CACHE_READ_TOKEN"], token)
    result = route(ctx, event, pr, status, lambda source, sha: has_s3_fixture(source, sha, token))
    if result["route"] == "cache":
        # Preserve the validation workflow's allowlisted, independently auditable
        # authorization receipt; neither tokens nor the complete API payload are logged.
        receipt = {"schema": 2, "allowed": "true", "head_sha": result["head_sha"],
                   "head_repository": result["head_repository"], "pr_number": str(number),
                   "base_ref": pr["base"]["ref"],
                   "author": pr["user"]["login"], "project_cache_namespace": PROJECT,
                   "verified_membership_http_status": status, "membership_checks": checks,
                   "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        approved.c.atomic_json(Path(os.environ["RUNNER_TEMP"]) / "real-pr-authorization.json", receipt)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as f:
        for key, value in result.items():
            print(f"{key}={value}", file=f)


if __name__ == "__main__":
    main()
