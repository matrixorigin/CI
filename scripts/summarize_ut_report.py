import argparse
import collections
import json
from pathlib import Path
import re
import unicodedata


def is_panic_like(output):
    lowered = output.lower()
    return (
        "panic:" in lowered
        or "fatal error:" in lowered
        or "index out of range" in lowered
        or "data race" in lowered
    )


def append_tail(buffers, key, value, limit):
    if key not in buffers:
        buffers[key] = collections.deque(maxlen=limit)
    buffers[key].append(value)


def clean_log_line(value):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value)[:1024])
    return " ".join("".join(c if not unicodedata.category(c).startswith("C") else " "
                            for c in text).split())[:260]


def compact_failure_log(build_failures, test_failures, build_outputs, test_outputs, package_outputs, panic_like):
    named_failures = {(package, test) for _, package, test, _ in test_failures if test}
    package_failures = {package for _, package, test, _ in test_failures if not test}
    unmatched_panics = collections.defaultdict(list)
    for _, package, test, line in panic_like:
        if test and package in package_failures and (package, test) not in named_failures:
            unmatched_panics[package, test].append(line)
    named_packages = {package for package, _ in named_failures | unmatched_panics.keys()}
    entries = [(package, "build failed", build_outputs.get(package, [])) for _, package in build_failures]
    entries.extend((package, test, lines) for (package, test), lines in unmatched_panics.items())
    for _, package, test, _ in test_failures:
        if not test and package in named_packages:
            continue  # the named tests already explain this package's failure
        outputs = test_outputs.get((package, test), []) if test else package_outputs.get(package, [])
        panic = [line for _, pkg, case, line in panic_like if (pkg, case) == (package, test)]
        entries.append((package, test or "<package>", [*panic, *outputs]))
    lines = []
    for package, name, outputs in entries[:5]:
        lines.append(f"FAILED {clean_log_line(package)} / {clean_log_line(name)}")
        candidates = (clean_log_line(line) for output in outputs for line in output[:2048].splitlines())
        interesting = dict.fromkeys(line for line in candidates if (
            is_panic_like(line) or re.search(r"\.go:\d+(?::\d+)?:|\b(?:Error|Expected|Actual):", line)
        ))
        # Panic headlines are more useful here than repeated stack frames.
        ordered = sorted(interesting, key=lambda line: not is_panic_like(line))
        lines.extend(f"Reason: {line}" for line in ordered[:2])
    if len(entries) > 5:
        lines.append(f"{len(entries) - 5} more failure entries omitted.")
    return "\n".join(lines) + ("\n" if lines else "")


def summarize(report_path, max_output_lines, max_build_failures, max_test_failures, max_panic_lines, compact_output=None):
    actions = collections.Counter()
    build_failures = []
    test_failures = []
    panic_like_count = 0
    malformed = 0

    with report_path.open(errors="replace") as report:
        for line_no, line in enumerate(report, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue

            action = event.get("Action")
            actions[action] += 1
            package = event.get("Package") or event.get("ImportPath") or "<unknown>"
            test = event.get("Test")
            output = event.get("Output")

            if action == "output" and output is not None and is_panic_like(output):
                panic_like_count += 1

            if action == "build-fail":
                build_failures.append((line_no, package))
            elif action == "fail":
                test_failures.append((line_no, package, test, event.get("Elapsed")))

    failed_packages = {package for _, package in build_failures}
    failed_packages.update(package for _, package, _, _ in test_failures)
    failed_tests = {(package, test) for _, package, test, _ in test_failures if test}
    build_outputs = {}
    package_outputs = {}
    test_outputs = {}
    panic_like = []

    with report_path.open(errors="replace") as report:
        for line_no, line in enumerate(report, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            action = event.get("Action")
            package = event.get("Package") or event.get("ImportPath") or "<unknown>"
            test = event.get("Test")
            output = event.get("Output")
            if output is None:
                continue
            output = output.rstrip("\n")

            if action == "build-output" and package in failed_packages:
                append_tail(build_outputs, package, output, max_output_lines)
            elif action == "output":
                if package in failed_packages:
                    append_tail(package_outputs, package, output, max_output_lines)
                if (package, test) in failed_tests:
                    append_tail(test_outputs, (package, test), output, max_output_lines)

                if is_panic_like(output) and (not failed_packages or package in failed_packages):
                    if len(panic_like) < max_panic_lines:
                        panic_like.append((line_no, package, test, output))

    if compact_output is not None:
        try:
            compact_output.write_text(compact_failure_log(
                build_failures, test_failures, build_outputs, test_outputs, package_outputs, panic_like
            ), encoding="utf-8")
        except OSError:
            print("Compact log summary could not be saved.")

    print("# Unit Test Failure Summary")
    print()
    print(f"- report: `{report_path}`")
    print(f"- events: `{dict(actions)}`")
    if malformed:
        print(f"- malformed json lines: `{malformed}`")
    print(f"- build failures: `{len(build_failures)}`")
    print(f"- failed test/package events: `{len(test_failures)}`")
    if panic_like_count:
        print(f"- panic-like output lines: `{panic_like_count}`")
    print()

    if build_failures:
        print("## Build Failures")
        print()
        for line_no, package in build_failures[:max_build_failures]:
            print(f"### {package}")
            print(f"- event line: `{line_no}`")
            lines = list(build_outputs.get(package, []))
            if lines:
                print("```text")
                print("\n".join(lines))
                print("```")
            else:
                print("_No build output was recorded for this package._")
            print()
        if len(build_failures) > max_build_failures:
            print(f"_Omitted {len(build_failures) - max_build_failures} additional build failures._")
            print()

    if test_failures:
        print("## Failed Tests And Packages")
        print()
        for line_no, package, test, elapsed in test_failures[:max_test_failures]:
            name = test or "<package>"
            print(f"### {package} / {name}")
            print(f"- event line: `{line_no}`")
            if elapsed is not None:
                print(f"- elapsed: `{elapsed}`")
            lines = test_outputs.get((package, test), []) if test else package_outputs.get(package, [])
            lines = list(lines)
            if lines:
                print("```text")
                print("\n".join(lines))
                print("```")
            else:
                print("_No test output was recorded for this failure event._")
            print()
        if len(test_failures) > max_test_failures:
            print(f"_Omitted {len(test_failures) - max_test_failures} additional failed test/package events._")
            print()

    if panic_like:
        print("## Panic-Like Output Samples")
        print()
        if failed_packages:
            print("_Only samples from failed packages are shown._")
            print()
        for line_no, package, test, output in panic_like:
            suffix = f" / {test}" if test else ""
            print(f"### line {line_no}: {package}{suffix}")
            print("```text")
            print(output)
            print("```")
            print()

    if not build_failures and not test_failures and not panic_like:
        print("No build-fail, fail, or panic-like output was found in the report.")


def main():
    parser = argparse.ArgumentParser(description="Summarize go test -json UT reports.")
    parser.add_argument("report", type=Path)
    parser.add_argument("--max-output-lines", type=int, default=80)
    parser.add_argument("--max-build-failures", type=int, default=10)
    parser.add_argument("--max-test-failures", type=int, default=10)
    parser.add_argument("--max-panic-lines", type=int, default=20)
    parser.add_argument("--compact-output", type=Path)
    args = parser.parse_args()

    summarize(
        args.report,
        args.max_output_lines,
        args.max_build_failures,
        args.max_test_failures,
        args.max_panic_lines,
        args.compact_output,
    )


if __name__ == "__main__":
    main()
