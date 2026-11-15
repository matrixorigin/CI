#!/usr/bin/env python3
"""Render a bounded, human-readable coverage UT incident report."""

from __future__ import annotations

import argparse
from collections import Counter, deque
import heapq
import json
from pathlib import Path
import re


INTERESTING_OUTPUT = re.compile(
    r"panic:|fatal error|--- FAIL:|^FAIL\s|test timed out|deadline exceeded",
    re.IGNORECASE | re.MULTILINE,
)
MAX_MARKDOWN_BYTES = 64 * 1024
MAX_AUXILIARY_LINE_BYTES = 1024


def read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def clean_inline(value: object, limit: int = 240) -> str:
    text = str(value if value is not None else "")
    text = " ".join(text.split())
    return text.replace("`", "'")[:limit]


def clean_code(value: str, limit: int = 1200) -> str:
    value = value.replace("```", "'''")
    if len(value) > limit:
        value = value[:limit] + "…"
    return value.rstrip()


def clean_auxiliary_line(value: str) -> str:
    line = value.rstrip("\r\n").replace("```", "'''")
    encoded = line.encode("utf-8")
    if len(encoded) > MAX_AUXILIARY_LINE_BYTES:
        line = encoded[:MAX_AUXILIARY_LINE_BYTES].decode("utf-8", errors="ignore") + "…"
    return line


def cap_markdown(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_MARKDOWN_BYTES:
        return value
    note = "\n\n> Report truncated at 64 KiB; inspect the raw artifact for complete evidence.\n"
    lines = value.splitlines(keepends=True)

    def select(prefix_budget: int) -> tuple[str, bool]:
        selected: list[str] = []
        used = 0
        for line in lines:
            line_bytes = len(line.encode("utf-8"))
            if used + line_bytes > prefix_budget:
                break
            selected.append(line)
            used += line_bytes
        prefix = "".join(selected).rstrip()
        return prefix, prefix.count("```") % 2 == 1

    # Keep complete lines so a cut can never leave a partial opening fence.
    # Always reserve closing-fence space before selecting the prefix. Whether
    # that space is ultimately needed is decided from the selected prefix, so
    # the budget cannot oscillate when a fence line sits at the boundary.
    closing_fence = "\n```"
    prefix_budget = max(
        0,
        MAX_MARKDOWN_BYTES
        - len(note.encode("utf-8"))
        - len(closing_fence.encode("utf-8")),
    )
    clipped, required_close = select(prefix_budget)
    suffix = (closing_fence if required_close else "") + note
    result = clipped + suffix
    assert len(result.encode("utf-8")) <= MAX_MARKDOWN_BYTES
    return result


def parse_report(
    path: Path,
    max_failures: int,
    max_excerpts: int,
    max_slow: int,
) -> tuple[Counter[str], list[dict[str, object]], list[str], list[tuple[float, str, str]], dict[str, object], int]:
    counts: Counter[str] = Counter()
    failures: list[dict[str, object]] = []
    excerpts: deque[str] = deque(maxlen=max_excerpts)
    slow: list[tuple[float, str, str]] = []
    last_event: dict[str, object] = {}
    malformed = 0
    if not path.is_file():
        return counts, list(failures), list(excerpts), slow, last_event, malformed
    with path.open("r", errors="replace") as stream:
        for raw_line in stream:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(event, dict):
                malformed += 1
                continue
            action = event.get("Action")
            if not isinstance(action, str):
                malformed += 1
                continue
            counts[action] += 1
            last_event = {
                key: event[key]
                for key in ("Action", "Package", "Test", "Time", "Elapsed")
                if key in event
            }
            package = event.get("Package") or "<unknown package>"
            test = event.get("Test") or "<package>"
            if action == "fail":
                if len(failures) < max_failures:
                    failures.append(
                        {
                            "package": package,
                            "test": test,
                            "elapsed": event.get("Elapsed"),
                        }
                    )
            output = event.get("Output")
            if isinstance(output, str) and INTERESTING_OUTPUT.search(output):
                excerpts.append(
                    f"{clean_inline(package)} / {clean_inline(test)}:\n{clean_code(output)}"
                )
            if action == "pass" and isinstance(event.get("Test"), str):
                try:
                    elapsed = float(event.get("Elapsed"))
                except (TypeError, ValueError):
                    elapsed = None
                if elapsed is not None:
                    item = (elapsed, str(package), str(test))
                    if len(slow) < max_slow:
                        heapq.heappush(slow, item)
                    elif item > slow[0]:
                        heapq.heapreplace(slow, item)
    return counts, failures, list(excerpts), slow, last_event, malformed


def read_tail_lines(path: Path, count: int) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", errors="replace") as stream:
        return list(deque(stream, maxlen=count))


def read_head_lines(path: Path, count: int) -> list[str]:
    if not path.is_file():
        return []
    lines = []
    with path.open("r", errors="replace") as stream:
        for line in stream:
            lines.append(line)
            if len(lines) >= count:
                break
    return lines


def render(args: argparse.Namespace) -> str:
    status = read_json(args.status)
    counts, failures, excerpts, slow, last_event, malformed = parse_report(
        args.report,
        args.max_failures,
        args.max_excerpts,
        args.max_slow,
    )
    report_bytes = args.report.stat().st_size if args.report.is_file() else 0
    result = clean_inline(status.get("result", "unknown"))
    reason = clean_inline(status.get("reason", "unknown"))
    lines = [
        "# Coverage UT incident report",
        "",
        f"- **Producer status:** `{result}`",
        f"- **Reason:** `{reason}`",
        f"- **Report size:** `{report_bytes}` bytes",
    ]
    if status.get("elapsed_seconds") is not None:
        lines.append(f"- **Runner elapsed:** `{status['elapsed_seconds']}` seconds")
    if status.get("exit_code") is not None:
        lines.append(f"- **Exit code:** `{status['exit_code']}`")

    lines.extend(["", "## Last observed event", ""])
    if last_event:
        lines.append(
            "- "
            + " / ".join(
                clean_inline(last_event.get(key))
                for key in ("Package", "Test", "Action")
                if key in last_event
            )
        )
    else:
        lines.append("No valid Go test JSON event was observed.")
    if malformed:
        lines.append(f"- Malformed or non-event JSON lines: `{malformed}`")

    lines.extend(["", "## Event counts", ""])
    if counts:
        lines.extend(f"- `{action}`: `{count}`" for action, count in sorted(counts.items()))
    else:
        lines.append("No Go test events were recorded.")

    lines.extend(["", "## Failed packages and tests", ""])
    if failures:
        for failure in failures:
            elapsed = failure.get("elapsed")
            suffix = f" ({clean_inline(elapsed)}s)" if elapsed is not None else ""
            lines.append(
                f"- `{clean_inline(failure.get('package'))}` / "
                f"`{clean_inline(failure.get('test'))}`{suffix}"
            )
    else:
        lines.append("No package/test failure event was recorded.")

    lines.extend(["", "## Error output excerpts", ""])
    if excerpts:
        for excerpt in excerpts:
            lines.extend(["```text", excerpt, "```"])
    else:
        lines.append("No panic, failure, or deadline excerpt was recorded.")

    lines.extend(["", "## Slowest completed tests", ""])
    if slow:
        for elapsed, package, test in sorted(slow, reverse=True):
            lines.append(f"- `{elapsed:.3f}s` `{clean_inline(package)}` / `{clean_inline(test)}`")
    else:
        lines.append("No completed test timing was recorded.")

    phase_lines = read_tail_lines(args.phase_timing, 80)
    if phase_lines:
        lines.extend(["", "## Phase timing", "", "```text"])
        lines.extend(clean_auxiliary_line(line) for line in phase_lines)
        lines.extend(["```"])

    progress_lines = read_tail_lines(args.progress, 20)
    if progress_lines:
        lines.extend(["", "## Recent progress", "", "```text"])
        lines.extend(clean_auxiliary_line(line) for line in progress_lines)
        lines.extend(["```"])

    timeout_lines = read_head_lines(args.timeout_diagnostics, 24)
    if timeout_lines:
        lines.extend(["", "## Timeout diagnostics preview", "", "```text"])
        lines.extend(clean_auxiliary_line(line) for line in timeout_lines)
        lines.extend(["```"])

    phase_failure_lines = read_head_lines(args.phase_failure, 24)
    if phase_failure_lines:
        lines.extend(["", "## Phase failure details", "", "```text"])
        lines.extend(clean_auxiliary_line(line) for line in phase_failure_lines)
        lines.extend(["```"])

    lines.extend(
        [
            "",
            "## Raw evidence",
            "",
            "The full JSON stream and runner diagnostics remain available in the uploaded artifact.",
        ]
    )
    return cap_markdown("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--phase-timing", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--timeout-diagnostics", type=Path, required=True)
    parser.add_argument("--phase-failure", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-failures", type=int, default=50)
    parser.add_argument("--max-excerpts", type=int, default=20)
    parser.add_argument("--max-slow", type=int, default=30)
    args = parser.parse_args()
    for name in ("max_failures", "max_excerpts", "max_slow"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
