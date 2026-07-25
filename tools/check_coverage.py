"""Enforce the per-file coverage floor that `--cov-fail-under` can only apply globally.

Reads a coverage JSON report and fails if any measured file is below the floor on
either line or branch coverage. Lives outside the packages under test so it is not
itself measured.

Usage: python tools/check_coverage.py coverage.json [--floor 80]
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_FLOOR = 80.0


def _percent(covered: int, total: int) -> float:
    # A file with no branches is vacuously at 100% branch coverage, not 0%.
    return 100.0 if total == 0 else 100.0 * covered / total


def shortfalls(report: dict, floor: float) -> list[tuple[str, float, float]]:
    failures = []
    for name, data in sorted(report.get("files", {}).items()):
        summary = data.get("summary", {})
        lines = _percent(summary.get("covered_lines", 0), summary.get("num_statements", 0))
        branches = _percent(summary.get("covered_branches", 0), summary.get("num_branches", 0))
        if lines < floor or branches < floor:
            failures.append((name, lines, branches))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="coverage JSON report (coverage json -o ...)")
    parser.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    args = parser.parse_args(argv)

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read coverage report {args.report}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(report, dict) or not report.get("files"):
        print(f"coverage report {args.report} measured no files", file=sys.stderr)
        return 2

    failures = shortfalls(report, args.floor)
    for name, lines, branches in failures:
        print(f"FAIL {name}: {lines:.0f}% lines, {branches:.0f}% branches (floor {args.floor:.0f}%)")
    if failures:
        print(f"\n{len(failures)} file(s) below the {args.floor:.0f}% coverage floor", file=sys.stderr)
        return 1

    print(f"all {len(report['files'])} measured file(s) meet the {args.floor:.0f}% line and branch floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
