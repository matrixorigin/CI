#!/usr/bin/env python3

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path


VALID_PHASES = {"serial-before", "parallel", "serial-after"}
SERIAL_CONTENT_RULES = (
    ("CREATE ACCOUNT", re.compile(r"\bCREATE\s+ACCOUNT\b", re.IGNORECASE)),
    ("DROP ACCOUNT", re.compile(r"\bDROP\s+ACCOUNT\b", re.IGNORECASE)),
    ("ALTER ACCOUNT", re.compile(r"\bALTER\s+ACCOUNT\b", re.IGNORECASE)),
    ("RESTORE ACCOUNT", re.compile(r"\bRESTORE\s+ACCOUNT\b", re.IGNORECASE)),
    ("SHOW ACCOUNTS", re.compile(r"\bSHOW\s+ACCOUNTS\b", re.IGNORECASE)),
    (
        "@session explicit credentials",
        re.compile(
            r"^\s*--\s*@session:[^\n]*(?:&user=|&password=)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    ("mo_ctl", re.compile(r"\bmo_ctl\s*\(", re.IGNORECASE)),
    (
        "mo_feature_registry",
        re.compile(r"\bmo_feature_registry_[A-Za-z0-9_]*", re.IGNORECASE),
    ),
    ("SET GLOBAL", re.compile(r"\bSET\s+GLOBAL\b", re.IGNORECASE)),
    (
        "@system command",
        re.compile(r"^\s*--\s*@system\b", re.IGNORECASE | re.MULTILINE),
    ),
    (
        "system metrics or debug",
        re.compile(r"\b(?:system_metrics|mo_debug)\b", re.IGNORECASE),
    ),
    (
        "account_id = 0",
        re.compile(r"\baccount_id\s*=\s*0\b", re.IGNORECASE),
    ),
    (
        "current account identity",
        re.compile(r"\bcurrent_account_(?:id|name)\s*\(", re.IGNORECASE),
    ),
    (
        "mo_catalog.mo_account",
        re.compile(r"\bmo_catalog\s*\.\s*mo_account\b", re.IGNORECASE),
    ),
    (
        "KILL CONNECTION or QUERY",
        re.compile(r"\bKILL\s+(?:CONNECTION|QUERY)\b", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class PolicyEntry:
    phase: str
    reason: str


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_policy(path: Path) -> dict[str, PolicyEntry]:
    with path.open() as policy_file:
        raw = json.load(policy_file, object_pairs_hook=_reject_duplicate_keys)
    if raw.get("schema_version") != 1:
        raise ValueError("policy schema_version must be 1")
    directories = raw.get("directories")
    if not isinstance(directories, dict):
        raise ValueError("policy directories must be an object")

    policy = {}
    for name, value in directories.items():
        if not name or "/" in name or "\\" in name:
            raise ValueError(f"invalid top-level directory name: {name}")
        if not isinstance(value, dict):
            raise ValueError(f"policy entry for {name} must be an object")
        phase = value.get("phase")
        reason = value.get("reason")
        if phase not in VALID_PHASES:
            raise ValueError(f"invalid phase for {name}: {phase}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"policy reason for {name} must be non-empty")
        policy[name] = PolicyEntry(phase=phase, reason=reason.strip())
    return policy


def _scripts_below(directory: Path) -> set[Path]:
    return {
        path.resolve()
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in {".sql", ".test"}
    }


def _serial_content_match(directory: Path, scripts: set[Path]) -> str | None:
    for path in sorted(scripts):
        content = path.read_text(errors="replace")
        for label, pattern in SERIAL_CONTENT_RULES:
            if pattern.search(content):
                relative = path.relative_to(directory).as_posix()
                return f"{label} in {relative}"
    return None


def _selected_by_directory(case_root: Path, selected_file: Path) -> dict[str, set[Path]]:
    selected = {}
    for line_number, raw_line in enumerate(selected_file.read_text().splitlines(), 1):
        if not raw_line.strip():
            continue
        path = Path(raw_line.strip()).resolve()
        try:
            relative = path.relative_to(case_root)
        except ValueError as error:
            raise ValueError(
                f"selected file outside case root at line {line_number}: {path}"
            ) from error
        if len(relative.parts) < 2:
            raise ValueError(f"selected path is not below a top-level directory: {path}")
        if not path.is_file() or path.suffix not in {".sql", ".test"}:
            raise ValueError(f"selected path is not a .sql or .test file: {path}")
        selected.setdefault(relative.parts[0], set()).add(path)
    if not selected:
        raise ValueError("selected file list is empty")
    return selected


def build_plan(
    case_root: Path,
    selected_file: Path,
    policy: dict[str, PolicyEntry],
    workers: int,
) -> dict:
    if workers < 1 or workers > 4:
        raise ValueError("workers must be between 1 and 4")
    case_root = case_root.resolve()
    if not case_root.is_dir():
        raise ValueError(f"case root is not a directory: {case_root}")
    selected_by_directory = _selected_by_directory(case_root, selected_file)

    units = []
    for name in sorted(selected_by_directory):
        directory = (case_root / name).resolve()
        if directory.parent != case_root or not directory.is_dir():
            raise ValueError(f"selected top-level directory is invalid: {name}")
        discovered = _scripts_below(directory)
        selected = selected_by_directory[name]
        if selected != discovered:
            missing = sorted(str(path) for path in discovered - selected)
            extra = sorted(str(path) for path in selected - discovered)
            detail = f"missing={missing[:3]}, extra={extra[:3]}"
            raise ValueError(f"partial directory selection for {name}: {detail}")

        entry = policy.get(name)
        reviewed = entry is not None
        if entry is None:
            entry = PolicyEntry(
                phase="serial-after",
                reason="unreviewed directory defaults to serial-after",
            )
        elif entry.phase == "parallel":
            serial_match = _serial_content_match(directory, discovered)
            if serial_match is not None:
                entry = PolicyEntry(
                    phase="serial-after",
                    reason=(
                        f"runtime content scan matched {serial_match}; "
                        "downgraded from parallel"
                    ),
                )
        units.append(
            {
                "name": name,
                "path": str(directory),
                "phase": entry.phase,
                "reason": entry.reason,
                "reviewed": reviewed,
                "script_count": len(discovered),
                "weight_bytes": sum(path.stat().st_size for path in discovered),
                "worker": None,
            }
        )

    worker_units = [
        {"index": index, "weight_bytes": 0, "directories": []}
        for index in range(workers)
    ]
    parallel_units = sorted(
        (unit for unit in units if unit["phase"] == "parallel"),
        key=lambda unit: (-unit["weight_bytes"], unit["name"]),
    )
    for unit in parallel_units:
        target = min(
            worker_units,
            key=lambda worker: (worker["weight_bytes"], worker["index"]),
        )
        target["directories"].append(unit["path"])
        target["weight_bytes"] += unit["weight_bytes"]
        unit["worker"] = target["index"]

    return {
        "schema_version": 1,
        "case_root": str(case_root),
        "selected_script_count": sum(unit["script_count"] for unit in units),
        "selected_directory_count": len(units),
        "serial_before": [
            unit["path"] for unit in units if unit["phase"] == "serial-before"
        ],
        "workers": worker_units,
        "serial_after": [
            unit["path"] for unit in units if unit["phase"] == "serial-after"
        ],
        "directories": units,
    }


def _write_include(path: Path, directories: list[str]) -> None:
    bounded_directories = [f"{directory.rstrip('/')}/" for directory in directories]
    path.write_text(
        ",".join(bounded_directories) + ("\n" if bounded_directories else "")
    )


def write_plan(plan: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n"
    )
    _write_include(output_dir / "serial-before.include", plan["serial_before"])
    for worker in plan["workers"]:
        _write_include(
            output_dir / f"worker-{worker['index']}.include",
            worker["directories"],
        )
    _write_include(output_dir / "serial-after.include", plan["serial_after"])

    with (output_dir / "inventory.tsv").open("w", newline="") as inventory_file:
        writer = csv.writer(inventory_file, delimiter="\t", lineterminator="\n")
        writer.writerow(
            [
                "directory",
                "phase",
                "worker",
                "reviewed",
                "script_count",
                "weight_bytes",
                "reason",
            ]
        )
        for unit in plan["directories"]:
            writer.writerow(
                [
                    unit["name"],
                    unit["phase"],
                    "" if unit["worker"] is None else unit["worker"],
                    str(unit["reviewed"]).lower(),
                    unit["script_count"],
                    unit["weight_bytes"],
                    unit["reason"],
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan tenant-parallel BVT execution by top-level case directory."
    )
    parser.add_argument("--case-root", required=True, type=Path)
    parser.add_argument("--selected-files", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--workers", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    policy = load_policy(args.policy)
    plan = build_plan(args.case_root, args.selected_files, policy, args.workers)
    write_plan(plan, args.output_dir)
    print(
        "BVT directory plan: "
        f"{plan['selected_directory_count']} directories, "
        f"{plan['selected_script_count']} scripts, "
        f"{len(plan['serial_before'])} serial-before, "
        f"{sum(len(worker['directories']) for worker in plan['workers'])} parallel, "
        f"{len(plan['serial_after'])} serial-after"
    )


if __name__ == "__main__":
    main()
