"""Exclusive admission for ONE coverage group. All writes are confined to trusted hosted jobs."""
import hashlib
import io
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlencode, urlsplit

REPO = "matrixorigin/matrixone"
API = "https://api.github.com/repos/" + REPO
WORKER = "coverage-standby-worker.yaml"
WORKER_REF = "main"
ENTRY = ".github/workflows/entrypoint.yaml"
# 4.2-dev must come from a fresh exact-base receipt from the opted-in caller.
# Schema 1 receipts still default to main and are not valid for 4.2-dev.
RECEIPT_BASE_REFS = frozenset({"main", "4.2-dev"})
POOLS = {"cs": "cs-node-cache-7c14g", "idc": "amd64-mo-shanghai-ut-safe"}
CS_SITES = ("mty", "jed", "mnl2", "ruh")
FIRST_POOL_MAX_AGE = 180
FIRST_POOL_PROBE_WAIT = 30
FIRST_CS_POOLS = frozenset(f"cs-{site}-nc-n{node}-pr-v1"
                           for site in CS_SITES for node in range(3)
                           if (site, node) != ("mty", 2))
CS_SITE_RETRY_AFTER = 300
CS_SITE_PROBE_WAIT = 90
WAIT = 3600
# Leave time to report a selection failure before the 85-minute workflow step ends.
SELECTOR_RESERVATION_SECONDS = 84 * 60
WORKER_READ_RECOVERY_SECONDS = 90
# A running or completed worker can outlast one GitHub quota window.
RESULT_RATE_LIMIT_RECOVERY_SECONDS = 60 * 60
PROGRESS_INTERVAL = 300
HANDOFF_POLLS = 60
HANDOFF_POLL_SECONDS = 5
DISPATCH_METADATA_POLLS = 24
WORKER_METADATA_POLL_SECONDS = 5
# One deadline for both identity and grant, inside the workflow's 20-minute step.
WORKER_ADMISSION_SECONDS = 19 * 60
ADMISSION_ARTIFACT_POLLS = 4
ADMISSION_ARTIFACT_WINDOW_SECONDS = 30
GRANT_POLL_SECONDS = 10
GRANT_SELECTOR_POLL_SECONDS = 30
RATE_LIMIT_FALLBACK_SECONDS = 60
ADMISSION_WINDOW = ContextVar("coverage_worker_admission", default=None)


def check(ok, msg):
    if not ok:
        raise ValueError(msg)


class AdmissionDeadlineExceeded(RuntimeError):
    pass


class AdmissionWindow:
    def __init__(self, deadline):
        self.deadline = deadline
        self.backoff = 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AdmissionDeadlineExceeded("admission deadline exceeded; no PR code executed")
        return remaining

    def rate_backoff(self, hint):
        delay, self.backoff = rate_limit_pause(hint, self.backoff)
        return delay


@contextmanager
def admission_window(deadline=None):
    # Worker reads share one deadline through checkout authorization. The
    # selector supplies a shorter deadline for its parent liveness check.
    window = ADMISSION_WINDOW.get()
    if window is not None:
        yield window
        return
    if deadline is None:
        deadline = time.monotonic() + WORKER_ADMISSION_SECONDS
    window = AdmissionWindow(deadline)
    token = ADMISSION_WINDOW.set(window)
    try:
        yield window
    finally:
        ADMISSION_WINDOW.reset(token)


def admission_remaining():
    window = ADMISSION_WINDOW.get()
    return window.remaining() if window is not None else float("inf")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def api(path, data=None, *, cache=None):
    check(path.startswith("/") and ".." not in path, "invalid API path")
    check(cache is None or data is None, "conditional requests are read-only")
    headers = {
        "Authorization": "Bearer " + os.environ["ACTIONS_TOKEN"],
        "Accept": "application/vnd.github+json", "Content-Type": "application/json"}
    cached = cache.get(path) if cache is not None else None
    if cached is not None:
        headers["If-None-Match"] = cached[0]
    req = urllib.request.Request(API + path, headers=headers,
        data=None if data is None else json.dumps(data).encode())
    try:
        response = urllib.request.build_opener(NoRedirect()).open(req, timeout=min(30, admission_remaining()))
    except urllib.error.HTTPError as error:
        if error.code != 304 or cached is None:
            raise
        with error:
            admission_remaining()
            return cached[1], parsedate_to_datetime(error.headers["Date"]).timestamp()
    with response as res:
        admission_remaining()
        raw = res.read()
        admission_remaining()
        payload = json.loads(raw) if raw else {}
        if cache is not None:
            etag = res.headers.get("ETag")
            if etag:
                cache[path] = (etag, payload)
            else:
                cache.pop(path, None)
        return payload, parsedate_to_datetime(res.headers["Date"]).timestamp()


def one(path):
    return api(path)[0]


def run(r):
    return one(f"/actions/runs/{int(r)}")


def digest(nonce):
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def title(t, pool):
    return "Coverage standby / {parent}/{attempt}/{group}/{head}/".format(**t) + pool + "/" + t["key"]


def short_title(t):
    return f"GROUP{t['group']}"


IDENTITY_SUFFIX = " / Run identity"


class WorkerIdentityPending(ValueError):
    pass


def identity_title(r, m):
    display = m["display_title"]
    if not re.fullmatch(r"GROUP[01]", display):
        return display
    data = one(f"/actions/runs/{r}/attempts/1/jobs?per_page=100")
    check(len(data["jobs"]) == data["total_count"], "incomplete worker identity response")
    marked = [j for j in data["jobs"] if j["name"].endswith(IDENTITY_SUFFIX)]
    if not marked:
        raise WorkerIdentityPending("worker identity is not indexed")
    check(len(marked) == 1, "ambiguous worker identity")
    marker = marked[0]
    check(marker["run_id"] == r and marker["run_attempt"] == 1, "invalid worker identity marker")
    canonical = marker["name"][:-len(IDENTITY_SUFFIX)]
    check(canonical.split("/")[3:4] == [display[-1]], "worker identity group differs from title")
    if marker["status"] in ("queued", "requested", "pending", "waiting", "in_progress") and marker["conclusion"] is None:
        raise WorkerIdentityPending("worker identity marker has not completed")
    check(marker["status"] == "completed" and marker["conclusion"] == "skipped",
          "invalid worker identity marker")
    return canonical


PARENT_POLLS = 13
PARENT_POLL_SECONDS = 5
# Job step names are part of the GitHub API's selector metadata.
GRANT_STEP = "Release exactly one worker"
WAIT_STEP = "Wait for selected group, including its UT result"
REVOKE_STEP = "Cancel the selected worker if the selector stops"


def parent_identity(t, p):
    expected = {
        "id": t["parent"], "run_attempt": t["attempt"], "repository.full_name": REPO,
        "path": ENTRY, "event": "pull_request_target",
        "head_repository.full_name": t["source"], "head_sha": t["head"],
    }
    for field, value in expected.items():
        actual = p
        for key in field.split("."):
            actual = actual.get(key) if isinstance(actual, dict) else None
        # Do not dump the API response, credentials, or the admission ticket.
        check(actual == value, f"parent {t['parent']} identity mismatch: {field}")


def selector_job(t, *, include_not_granted=False):
    """Find the exact group's selector, including an aborted grant if requested."""
    page = 1
    matches = []
    while True:
        admission_remaining()
        data = one(f"/actions/runs/{t['parent']}/attempts/{t['attempt']}/jobs?"
                   + urlencode(dict(per_page=100, page=page)))
        admission_remaining()
        jobs = data["jobs"]
        for job in jobs:
            # The matrix group and grant step identify the selector even if
            # its display name changes.
            names = job["name"].split(" / ")
            if (len(names) >= 2 and names[-2] == f"Route Coverage group {t['group']}"
                    and any(step["name"] == GRANT_STEP
                            and (include_not_granted or step.get("conclusion") in (None, "success"))
                            for step in (job.get("steps") or []))):
                check(job["run_id"] == t["parent"] and job["run_attempt"] == t["attempt"],
                      "selector identity mismatch")
                matches.append(job)
        if len(jobs) < 100:
            break
        page += 1
    check(len(matches) <= 1, "ambiguous parent selector")
    return matches[0] if matches else None


def selector_active(t):
    """Confirm this group's selector is still running in the parent attempt."""
    selector = selector_job(t)
    if selector is None:
        return False
    waits = [step for step in selector["steps"] if step["name"] == WAIT_STEP]
    return (selector["status"] == "in_progress"
            and selector["conclusion"] is None
            and len(waits) == 1 and waits[0].get("conclusion") != "skipped"
            and all("conclusion" in step
                    and step["conclusion"] in (None, "success", "skipped", "neutral")
                    for step in selector["steps"])
            and not any(
                step["name"] == REVOKE_STEP
                and step.get("status") in ("in_progress", "completed")
                and step["conclusion"] != "skipped"
                for step in selector["steps"]))


def retry_hint(error, headers, *, rate_limited):
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            delay = int(retry_after)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                raise error
    elif rate_limited and headers.get("X-RateLimit-Remaining") == "0":
        try:
            delay = int(headers["X-RateLimit-Reset"]) - time.time()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise error
    else:
        return None
    return max(PARENT_POLL_SECONDS, delay)


def throttle_headers(error, headers):
    return error.code == 429 or (error.code == 403 and
           (headers.get("Retry-After") is not None or headers.get("X-RateLimit-Remaining") == "0"))


def parent_retry_delay(error):
    """Return a safe delay for a transient parent metadata read."""
    if not isinstance(error, urllib.error.HTTPError):
        return PARENT_POLL_SECONDS
    headers = error.headers or {}
    rate_limited = throttle_headers(error, headers)
    if not rate_limited and error.code != 408 and not 500 <= error.code <= 599:
        raise error
    hint = retry_hint(error, headers, rate_limited=rate_limited)
    return hint or (RATE_LIMIT_FALLBACK_SECONDS if rate_limited else PARENT_POLL_SECONDS)


def confirmed_rate_limit_hint(error):
    """Return seconds from GitHub, zero for a throttle without a hint, or None."""
    headers = error.headers or {}
    if throttle_headers(error, headers):
        try:
            return retry_hint(error, headers, rate_limited=True) or 0
        except urllib.error.HTTPError:
            return 0
    if error.code == 403 and error.fp is not None:
        # The primary quota may still be positive during a secondary limit.
        # Only use the documented error message, never log the response body.
        admission_remaining()
        try:
            raw = error.read(4097)
            payload = json.loads(raw) if len(raw) <= 4096 else None
        except (OSError, ValueError):
            return None
        admission_remaining()
        message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(message, str):
            reason = message.casefold()
            if reason.startswith("api rate limit exceeded") or "secondary rate limit" in reason:
                return 0
    return None


def rate_limit_pause(hint, previous):
    if hint:
        return hint, 0
    backoff = max(RATE_LIMIT_FALLBACK_SECONDS, previous * 2)
    return backoff, backoff


def required_admission_read(read, *args, **kwargs):
    """Retry a confirmed throttle on a required read, never a permission error."""
    window = ADMISSION_WINDOW.get()
    if window is None:
        return read(*args, **kwargs)
    while True:
        window.remaining()
        try:
            result = read(*args, **kwargs)
        except urllib.error.HTTPError as error:
            hint = confirmed_rate_limit_hint(error)
            if hint is None:
                raise
            error.close()
            delay = window.rate_backoff(hint)
            if delay >= window.remaining():
                raise AdmissionDeadlineExceeded(
                    "GitHub cooldown exceeds the remaining admission budget; no PR code executed") from error
            print("GitHub throttled a required admission read; backing off", flush=True)
            time.sleep(delay)
        else:
            window.remaining()
            # A successful read ends the throttle streak across admission calls.
            window.backoff = 0
            return result


def parent_meta(t, require_selector=False, *, deadline=None):
    """Validate the identity and liveness of the parent attempt."""
    # A throttle can interrupt the parent re-read after selector validation.
    # Retry the whole check so a stale selector cannot authorize checkout.
    if deadline is None:
        return required_admission_read(_parent_meta, t, require_selector)
    with admission_window(min(deadline, time.monotonic() + HANDOFF_POLLS * HANDOFF_POLL_SECONDS)):
        return required_admission_read(_parent_meta, t, require_selector)


def _parent_meta(t, require_selector):
    # Polling and transient read failures share one bounded retry budget.
    deadline = time.monotonic() + min(PARENT_POLLS * PARENT_POLL_SECONDS, admission_remaining())
    status = None
    for poll in range(PARENT_POLLS):
        error = None
        delay = PARENT_POLL_SECONDS
        try:
            p = run(t["parent"])
            parent_identity(t, p)
            status, conclusion = p.get("status"), p.get("conclusion")
            check("conclusion" in p and conclusion is None,
                  f"parent {t['parent']} not active: conclusion={conclusion!r}")
            if status == "in_progress" and not require_selector:
                return p
            check(status in ("in_progress", "queued", "pending", "requested", "waiting"),
                  f"parent {t['parent']} not active: status={status!r}")
            # GitHub can report a queued run while one of its jobs is executing.
            # The selector stays active until its worker finishes, so it proves
            # that this group is still live without trusting another group's job.
            if selector_active(t):
                p = run(t["parent"])
                parent_identity(t, p)
                check("conclusion" in p and p["conclusion"] is None
                      and p.get("status") in ("in_progress", "queued", "pending", "requested", "waiting"),
                      f"parent {t['parent']} not active after selector check")
                return p
        except OSError as exc:
            if (ADMISSION_WINDOW.get() is not None and isinstance(exc, urllib.error.HTTPError)
                    and exc.code in (403, 429)):
                raise  # The admission window owns throttle recovery.
            error = exc
            delay = parent_retry_delay(exc)
        if poll == 0:
            reason = "metadata temporarily unavailable" if error is not None else f"status={status}"
            print(f"parent {t['parent']} {reason}; waiting for its selector", flush=True)
        if poll + 1 == PARENT_POLLS or time.monotonic() + delay > deadline:
            break
        time.sleep(delay)
    if error is not None:
        raise error
    raise ValueError(f"parent {t['parent']} still not active after {poll + 1} reads: status={status}")


class DispatchRevisionChanged(ValueError):
    def __init__(self, child, actual):
        super().__init__("dispatch resolved a different workflow revision")
        self.child, self.actual = child, actual


class WorkerDisplayTitleMismatch(ValueError):
    pass


def is_worker_run(r, m):
    return (m["id"] == r and m["repository"]["full_name"] == REPO and m["run_attempt"] == 1
            and m["path"] == ".github/workflows/" + WORKER and m["event"] == "workflow_dispatch"
            and m["head_branch"] == WORKER_REF)


def child_meta(r, t, pool, revision):
    """Read the exact worker and require its workflow revision when supplied."""
    m = run(r)
    check(is_worker_run(r, m), "dedicated worker identity mismatch")
    display = m.get("display_title")
    if display is None:
        raise WorkerDisplayTitleMismatch("dedicated worker identity mismatch: display title is missing")
    check(isinstance(display, str), "invalid worker display title")
    canonical = identity_title(r, m)
    expected = title(t, pool)
    if canonical == display and canonical != expected:
        raise WorkerDisplayTitleMismatch("dedicated worker identity mismatch: display title differs from ticket")
    check(canonical == expected and (display == canonical
          or (t.get("title_version") == 2 and display == short_title(t))),
          "dedicated worker identity mismatch")
    actual = m["head_sha"]
    check(re.fullmatch("[a-f0-9]{40}", actual), "invalid worker revision")
    if revision is not None and actual != revision:
        raise DispatchRevisionChanged(r, actual)
    return m


def reconcile_child_meta(r, t, pool, revision, *, wait_for_display_title):
    """Classify only retryable read failures, without accepting partial identity."""
    try:
        return child_meta(r, t, pool, revision)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        # Either the run record or its attempt-jobs metadata can index late.
        resource = ("attempt jobs" if urlsplit(error.geturl() or "").path.endswith("/attempts/1/jobs")
                    else "run record")
        error.close()
        raise WorkerIdentityPending(f"worker {resource} lookup returned HTTP 404") from error
    except WorkerDisplayTitleMismatch as error:
        if not wait_for_display_title:
            raise
        raise WorkerIdentityPending(str(error)) from error


def runner_matches_pool(name, pool):
    regex = (r"cs-(?:mty|mnl2|jed|ruh)-nc-n[012]-pr-v1-[a-z0-9]+-runner-[a-z0-9]+"
             if pool == "cs" else r"amd64-mo-shanghai-ut-safe-[a-z0-9]+-runner-[a-z0-9]+")
    return bool(re.fullmatch(regex, name) and not name.startswith("cs-mty-nc-n2-"))


def assigned(job, pool):
    runner_id = job.get("runner_id")
    return (type(runner_id) is int and runner_id > 0
            and runner_matches_pool(job.get("runner_name") or "", pool))


def first_cs_pool_plan(raw, now, *, parent=0):
    """Choose up to two distinct pools from a fresh trusted capacity receipt.

    slots is the number of whole CPU+memory+work-PVC placements on that node;
    pending is outstanding demand in that pool not yet charged to the node.
    This is an advisory snapshot, not a reservation across different PRs.
    """
    if not raw or len(raw) > 4096:
        return ()
    try:
        receipt = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(receipt, dict):
        return ()
    captured, pools = receipt.get("captured_at"), receipt.get("pools")
    if (type(captured) is not int or not now - FIRST_POOL_MAX_AGE <= captured <= now
            or not isinstance(pools, list) or len(pools) > len(FIRST_CS_POOLS)):
        return ()
    offers = {}
    for row in pools:
        if not isinstance(row, dict):
            return ()
        name, slots, pending = row.get("pool"), row.get("slots"), row.get("pending")
        if (not isinstance(name, str) or name not in FIRST_CS_POOLS or name in offers
                or type(slots) is not int or type(pending) is not int
                or not 0 <= slots <= 128 or not 0 <= pending <= 128):
            return ()
        offers[name] = slots - pending
    eligible = sorted(p for p, n in offers.items() if n > 0)
    if len(eligible) < 2:
        return tuple(eligible)
    # Stable within a parent (including the recheck before POST), but avoid
    # directing simultaneous PRs with equally good offers to one pool first.
    offset = parent % len(eligible)
    ranked = sorted(eligible[offset:] + eligible[:offset], key=lambda p: -offers[p])
    first = ranked[0]
    # Prefer distinct sites when possible; never nominate the same pool for
    # both groups of the parent.
    second = next((p for p in ranked[1:] if p.split("-")[1] != first.split("-")[1]), ranked[1])
    return first, second


def choose_initial_scale_set(t):
    # Only the protected, opted-in Coverage selectors can use a receipt.
    # The parent run check and the worker's independent grant/PR identity
    # checks still apply; capacity is not permission to execute PR code.
    if (type(t["pr"]) is not int or not 0 < t["pr"] < 10_000_000
            or t["attempt"] != 1 or t["group"] not in (0, 1)
            or os.environ.get("GITHUB_REPOSITORY") != REPO
            or os.environ.get("GITHUB_EVENT_NAME") != "pull_request_target"
            or os.environ.get("COVERAGE_CS_FIRST_POOL_ENABLED") != "true"):
        return None
    base, workflow = os.environ.get("GITHUB_BASE_REF"), os.environ.get("GITHUB_WORKFLOW_REF")
    main = f"{REPO}/{ENTRY}@refs/heads/main"
    release = f"{REPO}/{ENTRY}@refs/heads/4.2-dev"
    if not ((base == "main" and workflow == main) or
            (base == "4.2-dev" and workflow in (main, release)
             and os.environ.get("COVERAGE_ENABLE_4_2_DEV") == "true")):
        return None
    plan = first_cs_pool_plan(os.environ.get("COVERAGE_CS_FIRST_POOL_SNAPSHOT"), time.time(), parent=t["parent"])
    return plan[t["group"]] if t["group"] < len(plan) else None


def worker_labels(pool, site=None, scale_set=None):
    if scale_set is not None:
        check(pool == "cs" and site is None and isinstance(scale_set, str)
              and scale_set in FIRST_CS_POOLS, "invalid exact CS pool")
        return [scale_set]
    labels = [POOLS[pool]]
    if site is not None:
        check(pool == "cs" and site in CS_SITES, "invalid CS site")
        labels.append(f"cs-{site}-node-cache-all")
    return labels


def unassigned(job):
    return job is not None and job["status"] == "queued" and not (job.get("runner_id") or job.get("runner_name"))


def observe(r, t, pool, expect_visible=False, *, cache=None):
    get = api if cache is None else partial(api, cache=cache)
    try:
        data, now = get(f"/actions/runs/{r}/attempts/1/jobs?per_page=100")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        raise WorkerIdentityPending("worker attempt jobs lookup returned HTTP 404") from error
    check(len(data["jobs"]) == data["total_count"], "incomplete jobs response")
    target = []
    site, scale_set = t.get("site"), t.get("scale_set")
    for j in data["jobs"]:
        check(j["run_id"] == r and j["run_attempt"] == 1, "wrong job identity")
        if j["name"].split(" / ")[-1] == f"Coverage reservation {pool.upper()} group {t['group']}":
            check(sorted(j["labels"]) == sorted(worker_labels(pool, site, scale_set)), "wrong worker label")
            target.append(j)
        else:
            # The dispatcher has two mutually exclusive reusable calls. GitHub can
            # surface the non-selected call as a skipped placeholder.
            check(j["conclusion"] == "skipped", "not an exclusive single-group run")
    check(len(target) <= 1, "ambiguous reservation")
    if expect_visible and not target:
        raise WorkerIdentityPending("previously seen reservation job is not visible")
    job = target[0] if target else None
    if pool == "cs" and job is not None and job["status"] == "in_progress":
        name = job.get("runner_name")
        if name and site is not None:
            check(name.startswith(f"cs-{site}-"), "runner is outside requested CS site")
        if name and scale_set is not None:
            check(name.startswith(f"{scale_set}-"), "runner is outside requested CS pool")
        if not name or not job.get("runner_id"):
            check(not name or runner_matches_pool(name, pool), "unverified CS assignment")
            # GitHub can expose in_progress before indexing both runner fields.
            raise WorkerIdentityPending("CS runner assignment is not yet visible")
    return job, now


def decide(job, now, since):
    if job is None:
        check(since is None, "previously queued job disappeared")
        return None, None
    if job["status"] == "in_progress":
        check(assigned(job, "cs"), "unverified CS assignment")
        return "cs", since  # assignment wins, even at the exact deadline
    if job["status"] == "queued":
        if job.get("runner_id") or job.get("runner_name"):
            # GitHub can expose runner assignment before changing the status.
            # Preserve the observed queue start while this transition settles.
            return None, since
        since = now if since is None else since
        return ("idc" if now - since >= WAIT else None), since
    check(job["status"] in ("requested", "waiting", "pending"), "not a queue timeout")
    return None, since


def dispatch(t, pool, revision, deadline, *, remember):
    """Return the exact dispatched ID and its first CS observation (IDC: None)."""
    parent_meta(t, deadline=deadline)
    check(time.monotonic() < deadline, "selection deadline exceeded before dispatch")
    # Worker-ref and parent reads can outlive the capacity receipt. Finalize
    # the placement before sending the ticket, never after a worker exists.
    if pool == "cs" and "scale_set" in t and choose_initial_scale_set(t) != t["scale_set"]:
        del t["scale_set"]
    # At most one POST. If the response is lost, fail closed; no blind retries.
    body, dispatched_at = api(f"/actions/workflows/{WORKER}/dispatches", {
        "ref": WORKER_REF, "return_run_details": True,
        "inputs": {"ticket": json.dumps(t, separators=(",", ":")), "pool": pool}})
    returned_id = r = body.get("workflow_run_id")
    check(r is None or (type(r) is int and r > 0), "invalid dispatch run ID")
    if r is not None:
        remember(r)
    # A plain GROUP0/GROUP1 is shared across PRs. The dispatch time bound is
    # just a discovery filter; always compare the full identity below.
    recent = datetime.fromtimestamp(dispatched_at - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = min(deadline, time.monotonic() + DISPATCH_METADATA_POLLS * WORKER_METADATA_POLL_SECONDS)
    check(time.monotonic() < deadline, "dispatch metadata window elapsed; no worker granted")
    pending = None
    reported = set()
    for poll in range(DISPATCH_METADATA_POLLS):
        delay = WORKER_METADATA_POLL_SECONDS
        if not r:  # also support a legacy 204 response, correlated without guessing
            page = one(f"/actions/workflows/{WORKER}/runs?" + urlencode(
                dict(event="workflow_dispatch", branch=WORKER_REF, created=f">={recent}", per_page=100)))
            ids = []
            for candidate in page["workflow_runs"]:
                display = candidate.get("display_title")
                if display == title(t, pool):
                    ids.append(candidate["id"])
                elif t.get("title_version") == 2 and display == short_title(t):
                    try:
                        if identity_title(candidate["id"], candidate) == title(t, pool):
                            ids.append(candidate["id"])
                    except urllib.error.HTTPError as error:
                        if error.code not in (404, 408) and not 500 <= error.code <= 599:
                            raise
                        # This candidate may be another parent's worker. Only
                        # an exact ticket match can move discovery forward.
                        error.close()
                    except WorkerIdentityPending:
                        # A display name cannot identify a worker. Ignore an
                        # unindexed run on this scan; it cannot block an exact
                        # ticket match or receive a grant by itself.
                        continue
            check(len(ids) <= 1, "duplicate dispatch; refusing to select")
            r = ids[0] if ids else None
            if r is not None:
                remember(r)
        if r:
            try:
                reconcile_child_meta(r, t, pool, revision, wait_for_display_title=(r == returned_id))
                # A legacy full title can be indexed before its attempt jobs.
                # Keep the first CS observation in this same read-only budget.
                first_observation = observe(r, t, pool) if pool == "cs" else None
            except (OSError, WorkerIdentityPending) as error:
                delay, _ = metadata_read_retry(error)
                delay = delay or RATE_LIMIT_FALLBACK_SECONDS
                if isinstance(error, WorkerIdentityPending):
                    pending = str(error)
                elif isinstance(error, urllib.error.HTTPError):
                    pending = f"worker metadata returned HTTP {error.code}"
                else:
                    pending = "worker metadata transport unavailable"
                if pending not in reported:
                    reported.add(pending)
                    # Do not log the ticket, raw response, or API-supplied title.
                    print(f"worker {r} metadata not ready: {pending}; no grant yet", flush=True)
            else:
                check(time.monotonic() < deadline, "dispatch metadata window elapsed; no worker granted")
                print(f"{pool} group {t['group']}: https://github.com/{REPO}/actions/runs/{r}", flush=True)
                return r, first_observation
        if poll + 1 == DISPATCH_METADATA_POLLS or time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)
        if time.monotonic() >= deadline:
            break
        parent_meta(t, deadline=deadline)
        if time.monotonic() >= deadline:
            break
    if pending is not None:
        raise ValueError(f"worker {r} metadata not verified after {poll + 1} reads: "
                         f"{pending}; no worker granted")
    raise ValueError("dispatch not reconciled; inspect Actions before retrying")


def output(**fields):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        for k, v in fields.items():
            print(f"{k}={v}", file=f)


def reserve(t, pool, deadline):
    # Retry ONLY a positively identified, ungranted child resolved at the
    # wrong revision. Never retry an ambiguous dispatch or a lost response.
    for attempt in range(3):
        check(time.monotonic() < deadline, "selection deadline exceeded before reservation")
        branch = read_worker(f"{pool} worker ref", one, "/git/ref/heads/" + WORKER_REF, deadline=deadline)
        rev = t["revision"] = branch["object"]["sha"]
        check(re.fullmatch("[a-f0-9]{40}", rev), "invalid branch revision")
        nonce = secrets.token_hex(32)
        t["key"] = digest(nonce)

        def remember(child):
            # Keep local cleanup ownership even if selector verification times out.
            # Only grant.json can be published to authorize a worker.
            receipt = Path(os.environ["RUNNER_TEMP"], "reservation.json")
            staged = receipt.with_suffix(".tmp")
            staged.write_text(json.dumps(dict(ticket=t, child=child, pool=pool, nonce=nonce)))
            staged.replace(receipt)

        try:
            r, first_observation = dispatch(t, pool, rev, deadline, remember=remember)
            return r, rev, nonce, first_observation
        except DispatchRevisionChanged as changed:
            r = changed.child
            parent_meta(t, deadline=deadline)
            meta = child_meta(r, t, pool, changed.actual)
            if meta["status"] != "completed":
                try:
                    api(f"/actions/runs/{r}/cancel", {})
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
                for _ in range(30):
                    if child_meta(r, t, pool, changed.actual)["status"] == "completed":
                        break
                    time.sleep(2)
                else:
                    raise ValueError("ungranted worker did not stop; no redispatch")
            print(f"{pool} child {r} ended without a grant after branch movement", flush=True)
    raise ValueError("workflow revision kept changing; no worker granted")


def read_worker(phase, read, *args, deadline=float("inf"), retry_pending=True,
                rate_limit_budget=HANDOFF_POLLS * HANDOFF_POLL_SECONDS):
    """Use the handoff budget for control reads and a larger budget for results."""
    retryable = (OSError, WorkerIdentityPending) if retry_pending else OSError
    retry_started = None
    ordinary_deadline = None
    backoff = 0
    rate_backoff = 0
    while True:
        if deadline != float("inf") and time.monotonic() >= deadline:
            raise TimeoutError(f"{phase}: phase deadline exceeded")
        try:
            result = read(*args)
        except retryable as error:
            delay, rate_limited = metadata_read_retry(error)
            now = time.monotonic()
            if retry_started is None:
                retry_started = now
                if not rate_limited:
                    print(f"{phase}: metadata temporarily unavailable; retrying read", flush=True)
            retry_until = min(deadline, retry_started + rate_limit_budget)
            if rate_limited:
                ordinary_deadline = None
                delay, rate_backoff = rate_limit_pause(delay, rate_backoff)
            else:
                if ordinary_deadline is None:
                    ordinary_deadline = now + WORKER_READ_RECOVERY_SECONDS
                retry_until = min(retry_until, ordinary_deadline)
                # Indexing lag gets steady polls within the ordinary read budget.
                if isinstance(error, WorkerIdentityPending) or (
                        isinstance(error, urllib.error.HTTPError) and error.code == 404):
                    backoff = 0
                else:
                    delay = max(backoff, delay)
                    backoff = min(30, delay * 2)
            if now + delay < retry_until:
                if rate_limited:
                    print(f"{phase}: GitHub API throttled; retrying in {delay:.0f}s", flush=True)
                time.sleep(delay)
                if time.monotonic() < retry_until:
                    continue
            if rate_limited:
                raise TimeoutError(f"{phase}: GitHub cooldown ({delay:.0f}s) exceeds the remaining "
                                   f"read budget ({max(0, retry_until - now):.0f}s)") from error
            raise TimeoutError(f"{phase}: worker metadata did not recover within the retry budget") from error
        if deadline != float("inf") and time.monotonic() >= deadline:
            raise TimeoutError(f"{phase}: phase deadline exceeded")
        return result


def observe_cs_handoff(r, t, rev):
    parent_meta(t)
    child_meta(r, t, "cs", rev)
    return observe(r, t, "cs", True)


def withdraw_cs(r, t, rev, deadline):
    # Only used before publishing any grant. If GitHub assigns the runner
    # between the final read and cancellation, that runner cannot run PR code.
    check(time.monotonic() < deadline, "CS withdrawal deadline exceeded")
    try:
        api(f"/actions/runs/{r}/cancel", {})
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        # A concurrent cancellation can return 409. The conflict itself is
        # not an acknowledgement: verify this exact child in the loop below.
        exc.close()
    deadline = min(deadline, time.monotonic() + HANDOFF_POLLS * HANDOFF_POLL_SECONDS)
    for poll in range(HANDOFF_POLLS):
        if time.monotonic() >= deadline:
            break
        m = read_worker(f"confirming CS worker {r} cancellation", child_meta, r, t, "cs", rev, deadline=deadline)
        if m["status"] == "completed":
            check(m["conclusion"] == "cancelled", "CS did not acknowledge withdrawal")
            return
        if poll + 1 == HANDOFF_POLLS or time.monotonic() + HANDOFF_POLL_SECONDS >= deadline:
            break
        time.sleep(HANDOFF_POLL_SECONDS)
    raise ValueError("CS cancellation not confirmed; replacement not dispatched")


def select_cs(t, enable_site_retry, deadline):
    # Rotate the first choice on retries, without pinning normal work to any
    # site. A queued reservation is not proof that a site is out of capacity.
    offset = (t["parent"] + t["group"]) % len(CS_SITES)
    r, rev, nonce, (j, now) = reserve(t, "cs", deadline)
    cache = {}
    target = t.get("scale_set")
    remaining_sites = [*CS_SITES[offset:], *CS_SITES[:offset], None] if enable_site_retry else []
    first_queued = queued_here = last_queue_report = None
    if target:
        print(f"Group {t['group']}: first CS reservation targets {target} — {run_url(r)}", flush=True)
    else:
        print(f"Group {t['group']}: requesting a CS runner — {run_url(r)}", flush=True)
    while time.monotonic() < deadline:
        route, queued_here = decide(j, now, queued_here)
        if first_queued is None:
            first_queued = queued_here
        if unassigned(j) and now - first_queued >= WAIT:
            route = "idc"
        if route:
            print(f"group={t['group']} CS_job={j['id']} first_queued_utc={first_queued} decision_utc={now} route={route}", flush=True)
            return r, rev, nonce, route, first_queued
        exact_probe = "scale_set" in t
        threshold = CS_SITE_PROBE_WAIT if "site" in t else CS_SITE_RETRY_AFTER
        if exact_probe:
            threshold = FIRST_POOL_PROBE_WAIT
        if (exact_probe or remaining_sites) and unassigned(j) and now - queued_here >= threshold:
            j, now = read_worker(f"CS worker {r} handoff", observe_cs_handoff, r, t, rev, deadline=deadline)
            route, queued_here = decide(j, now, queued_here)
            if route == "cs":
                return r, rev, nonce, route, first_queued
            if unassigned(j):
                if now - first_queued >= WAIT:
                    return r, rev, nonce, "idc", first_queued
                withdraw_cs(r, t, rev, deadline)
                check(time.monotonic() < deadline, "CS selection deadline exceeded before replacement")
                if exact_probe:
                    old_pool = t.pop("scale_set")
                    notice = f"{old_pool} did not pick up the reservation; requesting the shared CS queue"
                elif (next_site := remaining_sites.pop(0)) is None:
                    del t["site"]
                    notice = "no CS site picked up the reservation in time; returning to the shared CS queue"
                else:
                    t["site"] = next_site
                    notice = f"trying CS {next_site.upper()}"
                # Keep first_queued across the entire sweep and the shared wait.
                queued_here = None
                r, rev, nonce, (j, now) = reserve(t, "cs", deadline)
                cache.clear()
                print(f"Group {t['group']}: {notice} — {run_url(r)}", flush=True)
                continue
        if first_queued is not None and (last_queue_report is None or now - last_queue_report >= PROGRESS_INTERVAL):
            print(f"Group {t['group']}: queued for a CS runner ({int(max(0, now - first_queued))}s) — {run_url(r)}", flush=True)
            last_queue_report = now
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(30, remaining))
        if time.monotonic() < deadline:
            j, now = read_worker(f"CS reservation {r}", partial(observe, cache=cache),
                                 r, t, "cs", queued_here is not None,
                                 deadline=deadline)
    status = j["status"] if j is not None else "job metadata unavailable"
    raise TimeoutError(f"CS reservation {r}: selection deadline exceeded ({status})")


def choose():
    deadline = time.monotonic() + SELECTOR_RESERVATION_SECONDS
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    check(0 < attempt < 1_000_000, "invalid parent attempt")
    t = dict(parent=int(os.environ["GITHUB_RUN_ID"]), attempt=attempt, group=int(os.environ["GROUP"]),
             head=os.environ["HEAD"], source=os.environ["SOURCE"], pr=int(os.environ["PR"]), title_version=2)
    check(t["group"] in (0, 1) and re.fullmatch("[a-f0-9]{40}", t["head"]), "invalid input")
    parent_meta(t, deadline=deadline)
    if attempt > 1:
        # A previous selector may have lost its connection after publishing a
        # grant. An old *ungranted* reservation cannot pass parent_meta; an
        # already admitted worker must have stopped before issuing a new grant.
        require_previous_grants_terminal(t)
    # A pull_request_target run reports the PR head; a workflow_dispatch run
    # reports the workflow branch head. These are not the same commit.
    pool = "cs"
    if initial_pool := choose_initial_scale_set(t):
        t["scale_set"] = initial_pool
    check(time.monotonic() < deadline, "selection deadline exceeded before CS reservation")
    r, rev, nonce, route, since = select_cs(t, os.environ.get("COVERAGE_CS_SITE_RETRY") == "true", deadline)
    if route == "idc":
        # A concurrently assigned CS worker is still blocked at admit: its nonce
        # was never published. Confirm identity again and cancel ONLY its own run.
        handoff_deadline = min(deadline, time.monotonic() + HANDOFF_POLLS * HANDOFF_POLL_SECONDS)
        route = None
        for poll in range(HANDOFF_POLLS):
            if time.monotonic() >= handoff_deadline:
                break
            j, now = read_worker(f"CS worker {r} handoff", observe_cs_handoff, r, t, rev,
                                 deadline=handoff_deadline)
            route, since = decide(j, now, since)
            if route is not None:
                break
            if poll + 1 == HANDOFF_POLLS or time.monotonic() + HANDOFF_POLL_SECONDS >= handoff_deadline:
                break
            time.sleep(HANDOFF_POLL_SECONDS)
        check(route is not None, "CS assignment did not settle at handoff; no worker granted")
        if route == "idc":
            withdraw_cs(r, t, rev, deadline)
            check(time.monotonic() < deadline, "selection deadline exceeded before Shanghai reservation")
            t.pop("site", None)
            t.pop("scale_set", None)
            pool = "idc"
            r, rev, nonce, _ = reserve(t, pool, deadline)
            print(f"Group {t['group']}: switched to Shanghai standby — {run_url(r)}", flush=True)
    # Grant is immutable and costs no extra control token on the worker. Until
    # this point neither pool has been authorized to check out or run PR code.
    parent_meta(t, deadline=deadline)
    check(time.monotonic() < deadline, "selection deadline exceeded before publishing a grant")
    Path(os.environ["RUNNER_TEMP"], "grant.json").write_text(json.dumps(
        dict(ticket=t, child=r, pool=pool, nonce=nonce)))
    placement = {"scale_set": t["scale_set"]} if "scale_set" in t else {}
    output(child=r, pool=pool, site=t.get("site", ""), **placement)


def artifact(parent, name, filename):
    data = one(f"/actions/runs/{parent}/artifacts?" + urlencode(dict(name=name, per_page=2)))
    items = data["artifacts"]
    if not items:
        return None
    check(len(items) == 1 and items[0]["name"] == name and not items[0]["expired"]
          and items[0]["size_in_bytes"] < 65536, "ambiguous or invalid control artifact")
    try:
        api(f"/actions/artifacts/{int(items[0]['id'])}/zip")
    except urllib.error.HTTPError as e:
        if e.code != 302:
            raise
        url = e.headers["Location"]
        e.close()
    else:
        raise ValueError("unexpected artifact response")
    check(urlsplit(url).scheme == "https", "unsafe download")
    # Fresh request with NO authorization header; do not print the signed URL.
    with urllib.request.urlopen(url, timeout=min(30, admission_remaining())) as res:
        admission_remaining()
        raw = res.read(65537)
        admission_remaining()
    check(len(raw) <= 65536, "control artifact too large")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        check(z.namelist() == [filename] and z.getinfo(filename).file_size <= 32768,
              "invalid control artifact payload")
        return json.loads(z.read(filename))


def metadata_read_retry(error):
    if isinstance(error, WorkerIdentityPending):
        return PARENT_POLL_SECONDS, False
    if isinstance(error, urllib.error.HTTPError):
        try:
            hint = confirmed_rate_limit_hint(error)
            if hint is not None:
                return hint, True
            if error.code == 404:
                return PARENT_POLL_SECONDS, False
            return parent_retry_delay(error), False
        finally:
            error.close()
    return parent_retry_delay(error), False


def admission_artifact(t, name, filename):
    """Allow briefly lagging artifact metadata without accepting a partial grant."""
    deadline = time.monotonic() + min(ADMISSION_ARTIFACT_WINDOW_SECONDS, admission_remaining())
    for poll in range(ADMISSION_ARTIFACT_POLLS):
        try:
            return required_admission_read(artifact, t["parent"], name, filename)
        except OSError as error:
            delay, _ = metadata_read_retry(error)
            delay = delay or RATE_LIMIT_FALLBACK_SECONDS
            if poll + 1 == ADMISSION_ARTIFACT_POLLS or time.monotonic() + delay > deadline:
                raise ValueError("parent admission artifact read unavailable; no PR code executed") from None
            time.sleep(delay)
            admission_remaining()
            parent_meta(t)


def check_grant_selector(t):
    """Stop only for a definitively failed selector of the exact parent attempt and group."""
    try:
        selector = selector_job(t, include_not_granted=True)
    except OSError as error:
        # A 403 on this optional lookup can be a secondary rate limit even
        # without Retry-After or a zero remaining count. Do not let it abort
        # admission, or keep using the same token during the cooldown. The
        # mandatory selector check after the grant still fails closed.
        cooldown = isinstance(error, urllib.error.HTTPError) and error.code in (403, 429)
        try:
            delay, _ = metadata_read_retry(error)
        except urllib.error.HTTPError:
            error.close()
            delay = 0 if cooldown else GRANT_SELECTOR_POLL_SECONDS
        return delay, cooldown
    except (AttributeError, KeyError, TypeError, ValueError):
        # Malformed or ambiguous metadata from a diagnostic probe is not
        # evidence that this group failed. Required checks still reject it.
        return GRANT_SELECTOR_POLL_SECONDS, False
    if selector and selector.get("status") == "completed":
        conclusion = selector.get("conclusion")
        if conclusion in ("failure", "cancelled", "timed_out", "skipped", "action_required", "startup_failure", "stale"):
            raise ValueError(f"parent {t['parent']} attempt {t['attempt']} group {t['group']} "
                             f"selector completed ({conclusion}) before worker admission; no PR code executed")
    return GRANT_SELECTOR_POLL_SECONDS, False


def worker_attempt(r, cache=None):
    path = f"/actions/runs/{r}/attempts/1"
    return one(path) if cache is None else api(path, cache=cache)[0]


def read_previous_grant(r):
    """Read and verify the worker's first attempt, which owns the grant."""
    m = worker_attempt(r)
    check(is_worker_run(r, m), "unrecognized previous grant; no redispatch")
    return m, identity_title(r, m)


def require_previous_grants_terminal(t):
    deadline = time.monotonic() + PARENT_POLLS * PARENT_POLL_SECONDS
    page = 1
    while True:
        path = f"/actions/runs/{t['parent']}/artifacts?per_page=100&page={page}"
        batch = read_worker("previous grant artifacts", one, path, deadline=deadline)["artifacts"]
        for item in batch:
            found = re.fullmatch(r"coverage-admit-([1-9][0-9]*)", item["name"])
            if not found:
                continue
            r = int(found[1])
            m, canonical = read_worker(f"previous grant worker {r}", read_previous_grant, r, deadline=deadline)
            fields = re.fullmatch(r"Coverage standby / ([1-9][0-9]*)/([1-9][0-9]*)/([01])/"
                                  r"([0-9a-f]{40})/(cs|idc)/[0-9a-f]{64}", canonical)
            check(fields and fields[1] == str(t["parent"]) and fields[4] == t["head"],
                  "unrecognized previous grant; no redispatch")
            if fields[3] == str(t["group"]):
                check(m["status"] == "completed", "previously granted group still active; no redispatch")
        if len(batch) < 100:
            return
        page += 1


def proof_name(t):
    if t["attempt"] == 1:
        return f"real-pr-snapshot-{t['parent']}-attempt-1"
    return f"real-pr-retry-{t['parent']}-attempt-{t['attempt']}-group-{t['group']}"


def wait_for_grant(t, r, pool):
    """Keep the exact worker alive through reconciliation, without reading a grant early."""
    with admission_window() as window:
        _wait_for_grant(t, r, pool, window)


def _wait_for_grant(t, r, pool, window):
    # The worker can start before the selector sees the dispatch response.
    # Spending time on identity reconciliation reduces the remaining grant wait.
    deadline = window.deadline
    identify = partial(reconcile_child_meta, r, t, pool, t["revision"], wait_for_display_title=True)
    verified = False
    next_liveness_check = 0
    while time.monotonic() < deadline:
        # The grant may arrive between liveness checks. Final admission
        # revalidates the parent and selector before any PR checkout.
        check_liveness = time.monotonic() >= next_liveness_check
        if check_liveness:
            parent_meta(t)
        if time.monotonic() >= deadline:
            break
        if not verified:
            try:
                # An unindexed worker goes through the selector liveness check below.
                read_worker(f"worker {r} identity", required_admission_read, identify,
                            deadline=deadline, retry_pending=False)
            except WorkerIdentityPending:
                pass
            else:
                verified = True
                parent_meta(t)
        if verified and time.monotonic() < deadline:
            g = admission_artifact(t, f"coverage-admit-{r}", "grant.json")
            if g is not None and time.monotonic() < deadline:
                check(g["child"] == r and g["ticket"] == t and g["pool"] == pool
                      and digest(g["nonce"]) == t["key"], "grant mismatch")
                return
        now = time.monotonic()
        if now >= deadline:
            break
        interval = GRANT_POLL_SECONDS if verified else WORKER_METADATA_POLL_SECONDS
        if check_liveness:
            delay, cooldown = check_grant_selector(t)
            if cooldown:
                interval = delay = max(interval, window.rate_backoff(delay))
                print(f"worker {r}: selector lookup returned 403/429; pausing admission reads", flush=True)
            else:
                window.backoff = 0
            now = time.monotonic()
            next_liveness_check = now + min(max(delay, GRANT_SELECTOR_POLL_SECONDS), deadline - now)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    reason = "no grant" if verified else "worker identity is unavailable"
    raise ValueError(f"{reason}; no PR code executed")


def admit():
    t = json.loads(os.environ["TICKET"])
    group, source, head, pool = int(os.environ["GROUP"]), os.environ["SOURCE"], os.environ["HEAD"], os.environ["POOL"]
    r = int(os.environ["GITHUB_RUN_ID"])
    check(os.environ["GITHUB_RUN_ATTEMPT"] == "1" and group == t["group"] and group in (0, 1)
          and head == t["head"] and source == t["source"] and pool in POOLS
          and type(t["attempt"]) is int and 0 < t["attempt"] < 1_000_000
          and type(t["parent"]) is int and type(t["pr"]) is int and re.fullmatch("[a-f0-9]{64}", t["key"]),
          "invalid worker ticket")
    site, scale_set = t.get("site"), t.get("scale_set")
    if scale_set is not None:
        worker_labels(pool, site, scale_set)
        check((os.environ.get("RUNNER_NAME") or "").startswith(f"{scale_set}-"),
              "runner is outside requested CS pool")
    if site is not None:
        worker_labels(pool, site)
        check((os.environ.get("RUNNER_NAME") or "").startswith(f"cs-{site}-"), "runner is outside requested CS site")
    check(re.fullmatch("[a-f0-9]{40}", t["revision"]), "invalid dispatch revision")
    with admission_window():
        wait_for_grant(t, r, pool)
        _finish_admission(t, source, head)


def _finish_admission(t, source, head):
    proof = admission_artifact(t, proof_name(t), "real-pr-authorization.json")
    pr = required_admission_read(one, f"/pulls/{int(t['pr'])}")
    # In-flight receipts from the old entrypoint retain their original
    # eligibility. New receipts bind the exact base and allow Draft PRs
    # independently of type labels.
    schema = proof.get("schema", 1) if proof else None
    base = proof.get("base_ref", "main" if schema == 1 else None) if proof else None
    check(proof and schema in (1, 2) and proof["allowed"] == "true" and proof["verified_membership_http_status"] == 204
          and int(proof["pr_number"]) == pr["number"] and proof["author"] == pr["user"]["login"]
          and proof["head_repository"] == source == pr["head"]["repo"]["full_name"]
          and proof["head_sha"] == head == pr["head"]["sha"] and pr["state"] == "open"
          and not pr["head"]["repo"]["archived"]
          and pr["base"]["repo"]["full_name"] == REPO
          and pr["base"]["ref"] == base and base in RECEIPT_BASE_REFS
          and (base == "main" or schema == 2)
          and (schema == 2 or (not pr["draft"] and "kind/test-ci" in {v["name"] for v in pr["labels"]})),
          "PR no longer matches authorization")
    # The selector can fail while the parent run remains active. Check the
    # exact selector again before the worker loads PR code.
    parent_meta(t, require_selector=True)
    admission_remaining()


def revoke():
    """Stop this selector's worker when selection, grant, or wait fails."""
    temp = Path(os.environ["RUNNER_TEMP"])
    path = temp / "grant.json"
    granted = path.exists()
    verify = child_meta
    if not granted:
        path = temp / "reservation.json"
        if not path.exists():
            return
        verify = partial(reconcile_child_meta, wait_for_display_title=True)
    receipt = json.loads(path.read_text())
    t, child, pool = receipt["ticket"], receipt["child"], receipt["pool"]
    selected = os.environ.get("CHILD")
    expected = dict(parent=int(os.environ["GITHUB_RUN_ID"]), attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
                    group=int(os.environ["GROUP"]), pr=int(os.environ["PR"]),
                    head=os.environ["HEAD"], source=os.environ["SOURCE"])
    check(all(t.get(key) == value for key, value in expected.items())
          and type(child) is int and child > 0 and (not selected or selected == str(child))
          and isinstance(t.get("revision"), str) and re.fullmatch("[a-f0-9]{40}", t["revision"])
          and pool in POOLS and digest(receipt["nonce"]) == t["key"], "invalid selector receipt")
    # The exact dispatch ticket owns an ungranted reservation even if main moved.
    revision = t["revision"] if granted else None
    deadline = time.monotonic() + WORKER_READ_RECOVERY_SECONDS
    rate_backoff = 0
    for attempt in range(3):
        meta = read_worker(f"revoking worker {child}", verify, child, t, pool, revision, deadline=deadline)
        if meta["status"] == "completed":
            print(f"coverage worker {child} is already complete", flush=True)
            return
        check(meta["status"] in ("queued", "pending", "requested", "waiting", "in_progress"),
              "worker has an unexpected status")
        try:
            api(f"/actions/runs/{child}/cancel", {})
            print(f"cancellation requested for coverage worker {child}", flush=True)
            return
        except OSError as error:
            if attempt == 2:
                raise
            delay = 2
            if isinstance(error, urllib.error.HTTPError):
                hint = confirmed_rate_limit_hint(error)
                if hint is not None:
                    delay, rate_backoff = rate_limit_pause(hint, rate_backoff)
                elif error.code != 409 and error.code < 500:
                    raise
                error.close()
            if time.monotonic() + delay >= deadline:
                raise
            time.sleep(delay)


def run_url(r):
    return f"https://github.com/{REPO}/actions/runs/{r}"


def worker_progress(r, group, pool, *, cache=None):
    t = {"group": group}
    if site := os.environ.get("COVERAGE_CS_SITE"):
        t["site"] = site
    if scale_set := os.environ.get("COVERAGE_CS_SCALE_SET"):
        t["scale_set"] = scale_set
    target, _ = observe(r, t, pool, cache=cache)
    if not target:
        return "waiting for the worker job", run_url(r)
    # Print only known phase text and canonical numeric URLs. Never relay output
    # from PR code into the trusted hosted dispatcher.
    url = f"{run_url(r)}/job/{int(target['id'])}"
    runner = target.get("runner_name") or ""
    site = None
    if assigned(target, "cs"):
        site = runner.split("-")[1].upper()
    elif assigned(target, "idc"):
        site = "Shanghai IDC"
    ut_step = {
        "cs": "Run coverage unit tests",
        "idc": "Run the reviewed Coverage UT group, cold build allowed",
    }[pool]
    ut = next((s.get("status") for s in (target.get("steps") or []) if s.get("name") == ut_step), None)
    if target.get("status") == "completed":
        phase = "worker job finished"
    elif ut == "completed":
        phase = "UT step finished; collecting results"
    elif ut == "in_progress":
        phase = "Coverage UT running (setup and tests)"
    elif site:
        phase = "runner assigned; preparing Coverage UT"
    else:
        phase = "waiting for a runner"
    return (f"{site}: {phase}" if site else phase), url


def wait():
    r = int(os.environ["CHILD"])
    pool = os.environ["POOL"]
    group = int(os.environ["GROUP"])
    print(f"Group {group}: open worker for live UT logs — {run_url(r)}", flush=True)
    next_progress_at = None
    # Poll cheaply. Dispatch, authorization and handoff use separate reads.
    cache = {}
    while True:
        m = read_worker(f"{pool} worker {r} result", worker_attempt, r, cache,
                        rate_limit_budget=RESULT_RATE_LIMIT_RECOVERY_SECONDS)
        check(m["id"] == r and m["run_attempt"] == 1, "selected worker attempt identity mismatch")
        if m["status"] == "completed":
            check(m["conclusion"] == "success", "selected coverage group did not pass")
            print(f"Group {group}: Coverage UT passed — {run_url(r)}", flush=True)
            return
        now = time.monotonic()
        if next_progress_at is None or now >= next_progress_at:
            next_progress_at = now + PROGRESS_INTERVAL
            try:
                phase, url = worker_progress(r, group, pool, cache=cache)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                phase, url = "worker details temporarily unavailable; still waiting", run_url(r)
            print(f"Group {group}: {phase} — {url}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    check(os.environ["GITHUB_REPOSITORY"] == REPO, "unexpected caller repository")
    {"choose": choose, "admit": admit, "wait": wait, "revoke": revoke}[sys.argv[1]]()
