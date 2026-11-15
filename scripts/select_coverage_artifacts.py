#!/usr/bin/env python3
"""Select one deterministic coverage artifact per producer generation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PRODUCERS = (
    "ut-coverage",
    "bvt-coverage-compose",
    "bvt-coverage-pessimistic",
)
ARTIFACT_NAME = re.compile(
    r"^(ut-coverage|bvt-coverage-compose|bvt-coverage-pessimistic)"
    r"-generation-(.+)-attempt-([1-9][0-9]*)$"
)


class SelectionError(RuntimeError):
    """The downloaded artifacts cannot form one coherent producer set."""


@dataclass(frozen=True)
class Candidate:
    producer: str
    generation: str
    attempt: int
    path: Path


def discover_candidates(download_dir: Path) -> list[Candidate]:
    candidates = []
    for path in sorted(download_dir.iterdir()):
        if not path.is_dir():
            continue
        match = ARTIFACT_NAME.fullmatch(path.name)
        if match is None:
            continue
        producer, generation, attempt = match.groups()
        candidates.append(Candidate(producer, generation, int(attempt), path))
    return candidates


def _complete_generations(candidates: Iterable[Candidate]) -> set[str]:
    by_producer = {producer: set() for producer in PRODUCERS}
    for candidate in candidates:
        by_producer[candidate.producer].add(candidate.generation)
    return set.intersection(*(by_producer[producer] for producer in PRODUCERS))


def select_candidates(
    candidates: Iterable[Candidate], expected_generation: str = ""
) -> dict[str, Candidate]:
    candidates = list(candidates)
    complete_generations = _complete_generations(candidates)
    if expected_generation:
        if expected_generation not in complete_generations:
            available = ", ".join(sorted(complete_generations)) or "none"
            raise SelectionError(
                f"generation {expected_generation!r} does not have all three "
                f"coverage producers; complete generations: {available}"
            )
        generation = expected_generation
    else:
        if len(complete_generations) != 1:
            available = ", ".join(sorted(complete_generations)) or "none"
            raise SelectionError(
                "expected exactly one complete coverage generation when no "
                f"generation was requested; found: {available}"
            )
        generation = next(iter(complete_generations))

    selected = {}
    for producer in PRODUCERS:
        matches = [
            candidate
            for candidate in candidates
            if candidate.producer == producer and candidate.generation == generation
        ]
        selected[producer] = max(matches, key=lambda candidate: candidate.attempt)
    return selected


def link_selected(selected: dict[str, Candidate], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    for producer in PRODUCERS:
        candidate = selected[producer]
        for source in sorted(candidate.path.rglob("*")):
            if source.is_symlink():
                raise SelectionError(
                    f"artifact {candidate.path.name!r} contains unsupported symlink "
                    f"{source.relative_to(candidate.path)}"
                )
            relative = source.relative_to(candidate.path)
            destination = output_dir / relative
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if destination.exists():
                raise SelectionError(
                    f"selected artifacts both contain {relative}; refusing an "
                    "order-dependent overwrite"
                )
            if not source.is_file():
                raise SelectionError(
                    f"artifact {candidate.path.name!r} contains unsupported "
                    f"non-regular file {relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Input and output both live under RUNNER_TEMP, so a hard link
            # isolates the selected namespace without copying large profiles.
            os.link(source, destination)


def selection_document(selected: dict[str, Candidate]) -> dict[str, object]:
    generation = selected[PRODUCERS[0]].generation
    return {
        "schema_version": 1,
        "generation": generation,
        "producers": {
            producer: {
                "artifact": selected[producer].path.name,
                "attempt": selected[producer].attempt,
            }
            for producer in PRODUCERS
        },
    }


def workflow_command_escape(value: object) -> str:
    return (
        str(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Select the highest run attempt for each coverage producer without "
            "merging stale artifacts into the same directory."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-generation", default="")
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    try:
        candidates = discover_candidates(args.input)
        selected = select_candidates(candidates, args.expected_generation)
        link_selected(selected, args.output)
        document = selection_document(selected)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(document, indent=2) + "\n")
    except (OSError, SelectionError) as error:
        print(
            "::error title=Coverage artifact selection::"
            f"{workflow_command_escape(error)}",
            file=sys.stderr,
        )
        return 1

    for producer in PRODUCERS:
        candidate = selected[producer]
        print(
            f"selected {producer}: {candidate.path.name} "
            f"(generation {candidate.generation}, attempt {candidate.attempt})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
