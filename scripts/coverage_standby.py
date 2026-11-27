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
CS_SITE_RETRY_AFTER = 300
CS_SITE_PROBE_WAIT = 90
WAIT = 3600
PROGRESS_INTERVAL = 300
JOB_DETAIL_INTERVAL = 120
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
SELECTOR_RATE_LIMIT_FALLBACK_SECONDS = 60
ADMISSION_WINDOW = ContextVar("coverage_worker_admission", default=None)


def check(ok, msg):
    if not ok:
        raise ValueError(msg)


class AdmissionDeadlineExceeded(RuntimeError):
    pass


class AdmissionWindow:
    def __init__(self):
        self.deadline = time.monotonic() + WORKER_ADMISSION_SECONDS
        self.backoff = 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AdmissionDeadlineExceeded("worker admission deadline exceeded; no PR code executed")
        return remaining

    def rate_backoff(self, delay):
        self.backoff = max(delay, self.backoff * 2)
        return self.backoff


@contextmanager
def admission_window():
    # Calls made by the worker share the deadline through the final authorization
    # check. Selector-side reads and writes do not use this worker budget.
    window = ADMISSION_WINDOW.get()
    if window is not None:
        yield window
        return
    window = AdmissionWindow()
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


def api(path, data=None):
    check(path.startswith("/") and ".." not in path, "invalid API path")
    req = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + os.environ["ACTIONS_TOKEN"],
        "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
        data=None if data is None else json.dumps(data).encode())
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=min(30, admission_remaining())) as res:
        admission_remaining()
        raw = res.read()
        admission_remaining()
        return json.loads(raw) if raw else {}, parsedate_to_datetime(res.headers["Date"]).timestamp()


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


def parent_retry_delay(error):
    """Return a safe delay for a transient parent metadata read."""
    if not isinstance(error, urllib.error.HTTPError):
        return PARENT_POLL_SECONDS
    headers = error.headers or {}
    retry_after = headers.get("Retry-After")
    rate_limited = error.code == 429 or (error.code == 403 and
                   (retry_after is not None or headers.get("X-RateLimit-Remaining") == "0"))
    if not rate_limited and error.code != 408 and not 500 <= error.code <= 599:
        raise error
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
        delay = 60 if rate_limited else PARENT_POLL_SECONDS
    return max(PARENT_POLL_SECONDS, delay)


def confirmed_rate_limit_delay(error):
    """Distinguish a GitHub throttle from a forbidden required read."""
    headers = error.headers or {}
    if error.code == 429 or (error.code == 403 and
                            (headers.get("Retry-After") is not None or headers.get("X-RateLimit-Remaining") == "0")):
        try:
            return parent_retry_delay(error)
        except urllib.error.HTTPError:
            return SELECTOR_RATE_LIMIT_FALLBACK_SECONDS
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
        if isinstance(message, str) and "secondary rate limit" in message.casefold():
            return SELECTOR_RATE_LIMIT_FALLBACK_SECONDS
    return None


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
            delay = confirmed_rate_limit_delay(error)
            if delay is None:
                raise
            error.close()
            backoff = window.rate_backoff(delay)
            print("GitHub throttled a required admission read; backing off", flush=True)
            time.sleep(min(backoff, window.remaining()))
        else:
            window.remaining()
            # A successful read ends the throttle streak across admission calls.
            window.backoff = 0
            return result


def parent_meta(t, require_selector=False):
    """Validate the identity and liveness of the parent attempt."""
    # A throttle can interrupt the parent re-read after selector validation.
    # Retry the whole check so a stale selector cannot authorize checkout.
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
                raise  # The admission window owns worker throttle handling.
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
    if actual != revision:
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


def assigned(job, pool):
    regex = (r"cs-(?:mty|mnl2|jed|ruh)-nc-n[012]-pr-v1-[a-z0-9]+-runner-[a-z0-9]+"
             if pool == "cs" else r"amd64-mo-shanghai-ut-safe-[a-z0-9]+-runner-[a-z0-9]+")
    return bool(job.get("runner_id") and re.fullmatch(regex, job.get("runner_name") or "")
                and not (job.get("runner_name") or "").startswith("cs-mty-nc-n2-"))


def worker_labels(pool, site=None):
    labels = [POOLS[pool]]
    if site is not None:
        check(pool == "cs" and site in CS_SITES, "invalid CS site")
        labels.append(f"cs-{site}-node-cache-all")
    return labels


def unassigned(job):
    return job is not None and job["status"] == "queued" and not (job.get("runner_id") or job.get("runner_name"))


def observe(r, t, pool):
    try:
        data, now = api(f"/actions/runs/{r}/attempts/1/jobs?per_page=100")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        raise WorkerIdentityPending("worker attempt jobs lookup returned HTTP 404") from error
    check(len(data["jobs"]) == data["total_count"], "incomplete jobs response")
    target = []
    for j in data["jobs"]:
        check(j["run_id"] == r and j["run_attempt"] == 1, "wrong job identity")
        if j["name"].split(" / ")[-1] == f"Coverage reservation {pool.upper()} group {t['group']}":
            site = t.get("site")
            check(sorted(j["labels"]) == sorted(worker_labels(pool, site)), "wrong worker label")
            if site is not None and j["status"] == "in_progress":
                check((j.get("runner_name") or "").startswith(f"cs-{site}-"), "runner is outside requested CS site")
            target.append(j)
        else:
            # The dispatcher has two mutually exclusive reusable calls. GitHub can
            # surface the non-selected call as a skipped placeholder.
            check(j["conclusion"] == "skipped", "not an exclusive single-group run")
    check(len(target) <= 1, "ambiguous reservation")
    return (target[0] if target else None), now


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


def dispatch(t, pool, revision):
    """Return the exact dispatched ID and its first CS observation (IDC: None)."""
    parent_meta(t)
    # At most one POST. If the response is lost, fail closed; no blind retries.
    body, dispatched_at = api(f"/actions/workflows/{WORKER}/dispatches", {
        "ref": WORKER_REF, "return_run_details": True,
        "inputs": {"ticket": json.dumps(t, separators=(",", ":")), "pool": pool}})
    returned_id = r = body.get("workflow_run_id")
    check(r is None or (type(r) is int and r > 0), "invalid dispatch run ID")
    # A plain GROUP0/GROUP1 is shared across PRs. The dispatch time bound is
    # just a discovery filter; always compare the full identity below.
    recent = datetime.fromtimestamp(dispatched_at - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = time.monotonic() + DISPATCH_METADATA_POLLS * WORKER_METADATA_POLL_SECONDS
    pending = None
    reported = set()
    for poll in range(DISPATCH_METADATA_POLLS):
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
        if r:
            try:
                reconcile_child_meta(r, t, pool, revision, wait_for_display_title=(r == returned_id))
                # A legacy full title can be indexed before its attempt jobs.
                # Keep the first CS observation in this same read-only budget.
                first_observation = observe(r, t, pool) if pool == "cs" else None
            except WorkerIdentityPending as error:
                pending = str(error)
                if pending not in reported:
                    reported.add(pending)
                    # Do not log the ticket, raw response, or API-supplied title.
                    print(f"worker {r} metadata not ready: {pending}; no grant yet", flush=True)
            else:
                check(time.monotonic() < deadline, "dispatch metadata window elapsed; no worker granted")
                print(f"{pool} group {t['group']}: https://github.com/{REPO}/actions/runs/{r}", flush=True)
                return r, first_observation
        if poll + 1 == DISPATCH_METADATA_POLLS or time.monotonic() + WORKER_METADATA_POLL_SECONDS >= deadline:
            break
        time.sleep(WORKER_METADATA_POLL_SECONDS)
        parent_meta(t)
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


def reserve(t, pool):
    # Retry ONLY a positively identified, ungranted child resolved at the
    # wrong revision. Never retry an ambiguous dispatch or a lost response.
    for attempt in range(3):
        rev = t["revision"] = one("/git/ref/heads/" + WORKER_REF)["object"]["sha"]
        check(re.fullmatch("[a-f0-9]{40}", rev), "invalid branch revision")
        nonce = secrets.token_hex(32)
        t["key"] = digest(nonce)
        try:
            r, first_observation = dispatch(t, pool, rev)
            return r, rev, nonce, first_observation
        except DispatchRevisionChanged as changed:
            r = changed.child
            parent_meta(t)
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


def withdraw_cs(r, t, rev):
    # Only used before publishing any grant. If GitHub assigns the runner
    # between the final read and cancellation, that runner cannot run PR code.
    try:
        api(f"/actions/runs/{r}/cancel", {})
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        # A concurrent cancellation can return 409. The conflict itself is
        # not an acknowledgement: verify this exact child in the loop below.
        exc.close()
    for _ in range(60):
        m = child_meta(r, t, "cs", rev)
        if m["status"] == "completed":
            check(m["conclusion"] == "cancelled", "CS did not acknowledge withdrawal")
            return
        time.sleep(5)
    raise ValueError("CS cancellation not confirmed; replacement not dispatched")


def select_cs(t, enable_site_retry):
    # Rotate the first choice on retries, without pinning normal work to any
    # site. A queued reservation is not proof that a site is out of capacity.
    offset = (t["parent"] + t["group"]) % len(CS_SITES)
    remaining_sites = [*CS_SITES[offset:], *CS_SITES[:offset], None] if enable_site_retry else []
    first_queued = queued_here = last_queue_report = None
    r, rev, nonce, (j, now) = reserve(t, "cs")
    print(f"Group {t['group']}: requesting a CS runner — {run_url(r)}", flush=True)
    while True:
        route, queued_here = decide(j, now, queued_here)
        if first_queued is None:
            first_queued = queued_here
        if unassigned(j) and now - first_queued >= WAIT:
            route = "idc"
        if route:
            print(f"group={t['group']} CS_job={j['id']} first_queued_utc={first_queued} decision_utc={now} route={route}", flush=True)
            return r, rev, nonce, route, first_queued
        threshold = CS_SITE_PROBE_WAIT if "site" in t else CS_SITE_RETRY_AFTER
        if remaining_sites and unassigned(j) and now - queued_here >= threshold:
            parent_meta(t)
            child_meta(r, t, "cs", rev)
            j, now = observe(r, t, "cs")
            route, queued_here = decide(j, now, queued_here)
            if route == "cs":
                return r, rev, nonce, route, first_queued
            if unassigned(j):
                if now - first_queued >= WAIT:
                    return r, rev, nonce, "idc", first_queued
                withdraw_cs(r, t, rev)
                next_site = remaining_sites.pop(0)
                if next_site is None:
                    del t["site"]
                    notice = "no CS site picked up the reservation in time; returning to the shared CS queue"
                else:
                    t["site"] = next_site
                    notice = f"trying CS {next_site.upper()}"
                # Keep first_queued across the entire sweep and the shared wait.
                queued_here = None
                r, rev, nonce, (j, now) = reserve(t, "cs")
                print(f"Group {t['group']}: {notice} — {run_url(r)}", flush=True)
                continue
        if first_queued is not None and (last_queue_report is None or now - last_queue_report >= PROGRESS_INTERVAL):
            print(f"Group {t['group']}: queued for a CS runner ({int(max(0, now - first_queued))}s) — {run_url(r)}", flush=True)
            last_queue_report = now
        time.sleep(30)
        j, now = observe(r, t, "cs")


def choose():
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    check(0 < attempt < 1_000_000, "invalid parent attempt")
    t = dict(parent=int(os.environ["GITHUB_RUN_ID"]), attempt=attempt, group=int(os.environ["GROUP"]),
             head=os.environ["HEAD"], source=os.environ["SOURCE"], pr=int(os.environ["PR"]), title_version=2)
    check(t["group"] in (0, 1) and re.fullmatch("[a-f0-9]{40}", t["head"]), "invalid input")
    parent_meta(t)
    if attempt > 1:
        # A previous selector may have lost its connection after publishing a
        # grant. An old *ungranted* reservation cannot pass parent_meta; an
        # already admitted worker must have stopped before issuing a new grant.
        require_previous_grants_terminal(t)
    # A pull_request_target run reports the PR head; a workflow_dispatch run
    # reports the workflow branch head. These are not the same commit.
    pool = "cs"
    r, rev, nonce, route, since = select_cs(t, os.environ.get("COVERAGE_CS_SITE_RETRY") == "true")
    if route == "idc":
        # A concurrently assigned CS worker is still blocked at admit: its nonce
        # was never published. Confirm identity again and cancel ONLY its own run.
        parent_meta(t)
        child_meta(r, t, "cs", rev)
        for poll in range(HANDOFF_POLLS):
            j, now = observe(r, t, "cs")
            route, since = decide(j, now, since)
            if route is not None:
                break
            if poll + 1 < HANDOFF_POLLS:
                time.sleep(HANDOFF_POLL_SECONDS)
                parent_meta(t)
                child_meta(r, t, "cs", rev)
        else:
            raise ValueError("CS assignment did not settle at handoff; no worker granted")
        if route == "idc":
            withdraw_cs(r, t, rev)
            t.pop("site", None)
            pool = "idc"
            r, rev, nonce, _ = reserve(t, pool)
            print(f"Group {t['group']}: switched to Shanghai standby — {run_url(r)}", flush=True)
    # Grant is immutable and costs no extra control token on the worker. Until
    # this point neither pool has been authorized to check out or run PR code.
    parent_meta(t)
    Path(os.environ["RUNNER_TEMP"], "grant.json").write_text(json.dumps(
        dict(ticket=t, child=r, pool=pool, nonce=nonce)))
    output(child=r, pool=pool, site=t.get("site", ""))


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


def admission_read_delay(error):
    if isinstance(error, urllib.error.HTTPError) and error.code == 404:
        error.close()
        return PARENT_POLL_SECONDS
    delay = parent_retry_delay(error)
    if isinstance(error, urllib.error.HTTPError):
        error.close()
    return delay


def admission_artifact(t, name, filename):
    """Allow briefly lagging artifact metadata without accepting a partial grant."""
    deadline = time.monotonic() + min(ADMISSION_ARTIFACT_WINDOW_SECONDS, admission_remaining())
    for poll in range(ADMISSION_ARTIFACT_POLLS):
        try:
            return required_admission_read(artifact, t["parent"], name, filename)
        except OSError as error:
            delay = admission_read_delay(error)
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
            delay = admission_read_delay(error)
        except urllib.error.HTTPError:
            error.close()
            delay = SELECTOR_RATE_LIMIT_FALLBACK_SECONDS if cooldown else GRANT_SELECTOR_POLL_SECONDS
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


def read_previous_grant(r, deadline):
    """Resolve a receipt's worker before deciding whether its group can rerun."""
    for poll in range(PARENT_POLLS):
        try:
            # Only the worker's first attempt can admit a grant. A later
            # GitHub rerun changes the latest run without changing that receipt.
            m = one(f"/actions/runs/{r}/attempts/1")
            check(is_worker_run(r, m), "unrecognized previous grant; no redispatch")
            return m, identity_title(r, m)
        except WorkerIdentityPending as error:
            reason = str(error)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            reason = "worker attempt or its jobs returned HTTP 404"
        if poll + 1 == PARENT_POLLS or time.monotonic() + PARENT_POLL_SECONDS >= deadline:
            break
        time.sleep(PARENT_POLL_SECONDS)
        if time.monotonic() >= deadline:
            break
    raise ValueError(f"previous grant worker {r} not verified: {reason}; no redispatch")


def require_previous_grants_terminal(t):
    deadline = time.monotonic() + PARENT_POLLS * PARENT_POLL_SECONDS
    page = 1
    while True:
        batch = one(f"/actions/runs/{t['parent']}/artifacts?per_page=100&page={page}")["artifacts"]
        for item in batch:
            found = re.fullmatch(r"coverage-admit-([1-9][0-9]*)", item["name"])
            if not found:
                continue
            m, canonical = read_previous_grant(int(found[1]), deadline)
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
    verified = False
    next_selector_check = 0
    while time.monotonic() < deadline:
        parent_meta(t)
        if time.monotonic() >= deadline:
            break
        if not verified:
            try:
                required_admission_read(reconcile_child_meta, r, t, pool, t["revision"], wait_for_display_title=True)
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
        if now >= next_selector_check:
            delay, cooldown = check_grant_selector(t)
            if cooldown:
                interval = delay = window.rate_backoff(max(delay, interval))
                print(f"worker {r}: selector lookup returned 403/429; pausing admission reads", flush=True)
            else:
                window.backoff = 0
            now = time.monotonic()
            next_selector_check = now + min(max(delay, GRANT_SELECTOR_POLL_SECONDS), deadline - now)
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
    site = t.get("site")
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
    """Stop this selector's worker after the grant or wait step fails."""
    grant = json.loads(Path(os.environ["RUNNER_TEMP"], "grant.json").read_text())
    t = grant["ticket"]
    child = int(os.environ["CHILD"])
    expected = dict(parent=int(os.environ["GITHUB_RUN_ID"]), attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
                    group=int(os.environ["GROUP"]), pr=int(os.environ["PR"]),
                    head=os.environ["HEAD"], source=os.environ["SOURCE"])
    check(all(t.get(key) == value for key, value in expected.items())
          and grant["child"] == child and grant["pool"] in POOLS
          and digest(grant["nonce"]) == t["key"], "invalid selector grant")
    for attempt in range(3):
        try:
            meta = child_meta(child, t, grant["pool"], t["revision"])
            if meta["status"] == "completed":
                print(f"selected coverage worker {child} is already complete", flush=True)
                return
            check(meta["status"] in ("queued", "pending", "requested", "waiting", "in_progress"),
                  "selected worker has an unexpected status")
            api(f"/actions/runs/{child}/cancel", {})
            print(f"cancellation requested for selected coverage worker {child}", flush=True)
            return
        except urllib.error.HTTPError as error:
            if error.code not in (409, 429) and error.code < 500:
                raise
            if attempt == 2:
                raise
        except OSError:
            if attempt == 2:
                raise
        time.sleep(2)


def run_url(r):
    return f"https://github.com/{REPO}/actions/runs/{r}"


def worker_progress(r, group, pool):
    t = {"group": group}
    if site := os.environ.get("COVERAGE_CS_SITE"):
        t["site"] = site
    target, _ = observe(r, t, pool)
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
    previous = None
    last_report = None
    next_details = None
    while True:
        m = run(r)
        if m["status"] == "completed":
            check(m["run_attempt"] == 1 and m["conclusion"] == "success", "selected coverage group did not pass")
            print(f"Group {group}: Coverage UT passed — {run_url(r)}", flush=True)
            return
        now = time.monotonic()
        if next_details is None or now >= next_details:
            next_details = now + JOB_DETAIL_INTERVAL
            try:
                progress = worker_progress(r, group, pool)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                progress = ("worker details temporarily unavailable; still waiting", run_url(r))
            if progress != previous or last_report is None or now - last_report >= PROGRESS_INTERVAL:
                phase, url = progress
                print(f"Group {group}: {phase} — {url}", flush=True)
                previous, last_report = progress, now
        time.sleep(30)


if __name__ == "__main__":
    check(os.environ["GITHUB_REPOSITORY"] == REPO, "unexpected caller repository")
    {"choose": choose, "admit": admit, "wait": wait, "revoke": revoke}[sys.argv[1]]()
