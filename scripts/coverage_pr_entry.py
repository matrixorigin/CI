"""Trusted event adapter. No dispatch, cache publication or PR-head execution."""
import datetime as dt
import json
import os
import re
from pathlib import Path

PROJECT = "matrixorigin/matrixone"
ENTRY = PROJECT + "/.github/workflows/entrypoint.yaml@refs/heads/main"
CACHE_BASE_REFS = frozenset({"main", "4.2-dev"})


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def route(ctx, event, live, member_status):
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
    labels = {row["name"] for row in live["labels"]}
    # Never use an arbitrary caller-selected head, runner label or environment.
    # The old path still enforces its own org/safe-to-test eligibility.
    if (base not in CACHE_BASE_REFS or live["draft"] or live["head"]["repo"]["archived"] or
            member_status != 204 or "kind/test-ci" not in labels):
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
    result = route(ctx, event, pr, status)
    if result["route"] == "cache":
        # Preserve the validation workflow's allowlisted, independently auditable
        # authorization receipt; neither tokens nor the complete API payload are logged.
        receipt = {"schema": 1, "allowed": "true", "head_sha": result["head_sha"],
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
