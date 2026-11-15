#!/usr/bin/env python3
"""Keep small, credential-redacted Coverage skip evidence on success and failure."""

import argparse
from collections import Counter, deque
import json
from pathlib import Path
import re


def redact(text):
    text = re.sub(r"https?://\S+", "<endpoint>", text)
    text = re.sub(r"\S+@tcp\([^)]*\)\S*", "<database-connection>", text)
    text = re.sub(r"(?i)((?:password|secret|token|access[_-]?key|api[_-]?key|key-id|key-secret)\s*[:=]\s*)[^\s,;]+", r"\1<redacted>", text)
    return text[:800]


def collect(report, status, source_sha, runner, mode):
    counts = Counter()
    started_packages = set()
    notes = {}
    skipped, packages, failed, timeouts = [], [], [], []
    malformed = 0
    with report.open(errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(event, dict) or not isinstance(event.get("Action"), str):
                malformed += 1
                continue
            action = event["Action"]
            counts[action] += 1
            if action == "start":
                started_packages.add(event.get("Package", ""))
            key = (event.get("Package", ""), event.get("Test", ""))
            item = {"package": key[0], "test": key[1]}
            if action == "output":
                message = event.get("Output", "").strip()
                # Keep only Go test messages, not arbitrary application logs.
                if re.match(r"[^\s]+\.go:\d+:", message):
                    notes.setdefault(key, deque(maxlen=8)).append(redact(message))
                if re.search(r"panic: test timed out after|^test timed out after", message):
                    timeouts.append(dict(item, reason=redact(message)))
            elif action == "skip":
                item["reasons"] = list(notes.pop(key, [])) or [
                    "skip without message" if key[1] else "package has no test files"
                ]
                (skipped if key[1] else packages).append(item)
            elif action in ("pass", "fail"):
                notes.pop(key, None)
                if action == "fail":
                    failed.append(item)
    return {
        "schema": 1, "source_sha": source_sha, "runner": runner, "storage_mode": mode,
        "producer_status": status.get("result", "unknown"),
        "producer_reason": status.get("reason", "unknown"),
        "elapsed_seconds": status.get("elapsed_seconds"),
        "malformed_events": malformed, "event_counts": dict(counts),
        "started_packages": sorted(started_packages),
        "skipped_tests": skipped, "packages_without_tests": packages,
        "failed": failed, "package_timeouts": timeouts,
        "note": "Real COS/OSS integration is intentionally outside CS UT scope. Absent dynamic storage cases do not appear as skip events.",
    }


def compare(baseline, candidate):
    def identities(items):
        return {(x["package"], x["test"]) for x in items}

    old = identities(baseline["skipped_tests"])
    new = identities(candidate["skipped_tests"])
    added = sorted(new - old)
    reasons_changed = []
    old_reasons = {(x["package"], x["test"]): x["reasons"] for x in baseline["skipped_tests"]}
    for item in candidate["skipped_tests"]:
        key = (item["package"], item["test"])
        if key in old_reasons and old_reasons[key] != item["reasons"]:
            reasons_changed.append(item)
    comparable = baseline["source_sha"] == candidate["source_sha"] and bool(baseline["source_sha"])
    same_packages = bool(baseline["started_packages"]) and baseline["started_packages"] == candidate["started_packages"]
    healthy = all(x["producer_status"] == "passed" and not x["malformed_events"]
                  and not x["failed"] and not x["package_timeouts"] for x in (baseline, candidate))
    return {
        "same_source": comparable, "same_package_scope": same_packages, "both_producers_passed": healthy,
        "new_skips": [{"package": p, "test": t} for p, t in added],
        "removed_skips": [{"package": p, "test": t} for p, t in sorted(old - new)],
        "changed_skip_reasons": reasons_changed,
        "baseline_skip_tests": len(old), "candidate_skip_tests": len(new),
        "accepted": comparable and same_packages and healthy and not added,
        "scope": "Existing skipped identities are accepted; real COS/OSS coverage is not required. Changed reasons are retained for review, not treated as new skips.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--runner", default="unknown")
    parser.add_argument("--mode", default="unknown")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.baseline and args.candidate:
        result = compare(json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()))
        exit_code = 0 if result["accepted"] else 1
    elif args.report and args.status and args.source_sha:
        result = collect(args.report, json.loads(args.status.read_text()), args.source_sha, args.runner, args.mode)
        exit_code = 0
    else:
        parser.error("provide report/status/source-sha or baseline/candidate")
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
