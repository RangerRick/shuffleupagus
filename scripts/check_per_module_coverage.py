#!/usr/bin/env python3
"""Fail if any module's coverage is below its own floor.

The aggregate fail_under in pyproject.toml is a whole-project number, so one
module can rot to 20% while another sits at 100% and the gate stays green.
Floors live in [tool.coverage-floors]; `default` applies to every module without
an explicit entry.

This is a gate, so it distinguishes "I found no violations" from "I could not
evaluate". Anything it cannot read or cannot parse is an error, never a pass.
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path

DEFAULT_FLOOR = 80


class CoverageError(Exception):
    """The report or the floor config could not be evaluated."""


def load_floors(pyproject: Path) -> tuple[float, dict[str, float]]:
    """Return (default_floor, {module_path: floor}) from [tool.coverage-floors]."""
    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CoverageError(f"cannot read floors from {pyproject}: {exc}") from exc

    table = dict(data.get("tool", {}).get("coverage-floors", {}))
    default = table.pop("default", DEFAULT_FLOOR)
    floors: dict[str, float] = {}
    for path, floor in table.items():
        if not isinstance(floor, int | float) or isinstance(floor, bool):
            raise CoverageError(f"floor for {path!r} is not a number: {floor!r}")
        floors[path] = float(floor)
    if not isinstance(default, int | float) or isinstance(default, bool):
        raise CoverageError(f"default floor is not a number: {default!r}")
    return float(default), floors


def load_coverage(report: Path) -> dict[str, float]:
    """Return {module_path: percent_covered} from a coverage.json report."""
    try:
        data = json.loads(report.read_text())
    except OSError as exc:
        raise CoverageError(f"cannot read {report}: run pytest with --cov-report=json:{report} first") from exc
    except json.JSONDecodeError as exc:
        raise CoverageError(f"{report} is not valid JSON: {exc}") from exc

    files = data.get("files")
    if not isinstance(files, dict):
        raise CoverageError(f"{report} has no 'files' object — coverage measured nothing")
    if not files:
        raise CoverageError(f"{report} lists zero measured files — coverage measured nothing")

    measured: dict[str, float] = {}
    for path, entry in files.items():
        percent = (entry or {}).get("summary", {}).get("percent_covered")
        if not isinstance(percent, int | float) or isinstance(percent, bool):
            raise CoverageError(f"{report} has a non-numeric coverage for {path}: {percent!r}")
        measured[path] = float(percent)
    return measured


def find_violations(
    measured: dict[str, float],
    default_floor: float,
    floors: dict[str, float],
) -> list[str]:
    """Return one message per problem. Empty means every module is at or above its floor."""
    problems = []
    for path, percent in sorted(measured.items()):
        floor = floors.get(path, default_floor)
        if percent < floor:
            problems.append(f"BELOW FLOOR  {percent:6.2f}% < {floor:g}%  {path}")

    # A floor for a module the report does not mention is not a pass. Re-adding a
    # path to [tool.coverage.run] omit would otherwise silently exempt it, and a
    # floor left behind by a rename would quietly become decoration.
    for path in sorted(floors):
        if path not in measured:
            problems.append(f"NOT MEASURED  floor set for {path}, but the report does not mention it")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", default="coverage.json", type=Path)
    parser.add_argument("--pyproject", default=Path("pyproject.toml"), type=Path)
    args = parser.parse_args(argv)

    try:
        default_floor, floors = load_floors(args.pyproject)
        measured = load_coverage(args.report)
    except CoverageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    problems = find_violations(measured, default_floor, floors)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print(
            "\nRaise the module's coverage, or adjust its floor in [tool.coverage-floors]\n"
            "with a comment saying why. Never lower a floor just to make a red run green.",
            file=sys.stderr,
        )
        return 1

    print(f"per-module coverage: {len(measured)} modules at or above their floor (default {default_floor:g}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
