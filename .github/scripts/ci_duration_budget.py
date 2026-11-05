#!/usr/bin/env python3
"""Report and enforce a wall-clock budget for a CI job path."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import NamedTuple

from booley.core.boundary import require_int


class DurationResult(NamedTuple):
    label: str
    elapsed_seconds: int
    budget_seconds: int
    passed: bool
    warn_seconds: int | None = None
    warned: bool = False


def evaluate(
    label: str, *, started_at: int, now: int, budget: int, warn: int | None = None
) -> DurationResult:
    """Evaluate one measured duration against its fail budget and optional warn threshold.

    ``budget`` is the fail threshold. ``warn``, when given, must be positive and
    not exceed ``budget``; a run over ``warn`` but within ``budget`` passes with
    ``warned`` set.
    """
    started_at = require_int(started_at, field="started_at")
    now = require_int(now, field="now")
    budget = require_int(budget, field="budget")
    if warn is not None:
        warn = require_int(warn, field="warn")
    if not label.strip():
        raise ValueError("label must not be empty")
    if started_at <= 0:
        raise ValueError("started_at must be positive")
    if budget <= 0:
        raise ValueError("budget must be positive")
    if warn is not None and warn <= 0:
        raise ValueError("warn must be positive")
    if warn is not None and warn > budget:
        raise ValueError("warn must not exceed budget")
    if now < started_at:
        raise ValueError("now precedes started_at")
    elapsed = now - started_at
    passed = elapsed <= budget
    warned = passed and warn is not None and elapsed > warn
    return DurationResult(label, elapsed, budget, passed, warn, warned)


def summary_line(result: DurationResult) -> str:
    """Render one stable Markdown summary row."""
    if not result.passed:
        verdict = "**FAIL**"
    elif result.warned and result.warn_seconds is not None:
        over = result.elapsed_seconds - result.warn_seconds
        verdict = f"**WARN** (+{over}s over warn)"
    else:
        verdict = "**PASS**"
    warn_note = f" (warn {result.warn_seconds}s)" if result.warned else ""
    return (
        f"- `{result.label}` duration: {result.elapsed_seconds}s / "
        f"{result.budget_seconds}s budget{warn_note} — {verdict}\n"
    )


def annotation(result: DurationResult) -> str | None:
    """Return the GitHub ``::warning`` workflow command for a warned run, else ``None``."""
    if not result.warned:
        return None
    return (
        f"::warning title=Duration budget::{result.label} took {result.elapsed_seconds}s, "
        f"over the {result.warn_seconds}s warn threshold (fail at {result.budget_seconds}s)"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--started-at-epoch", required=True, type=int)
    parser.add_argument("--budget-seconds", required=True, type=int)
    parser.add_argument("--warn-seconds", type=int)
    parser.add_argument("--now-epoch", type=int)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    now = args.now_epoch if args.now_epoch is not None else int(time.time())
    result = evaluate(
        args.label,
        started_at=args.started_at_epoch,
        now=now,
        budget=args.budget_seconds,
        warn=args.warn_seconds,
    )
    line = summary_line(result)
    warning = annotation(result)
    if warning is not None:
        print(warning)
    print(line, end="")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write(line)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
