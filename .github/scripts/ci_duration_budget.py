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


def evaluate(label: str, *, started_at: int, now: int, budget: int) -> DurationResult:
    """Evaluate one measured duration against its positive wall-clock budget."""
    started_at = require_int(started_at, field="started_at")
    now = require_int(now, field="now")
    budget = require_int(budget, field="budget")
    if not label.strip():
        raise ValueError("label must not be empty")
    if started_at <= 0:
        raise ValueError("started_at must be positive")
    if budget <= 0:
        raise ValueError("budget must be positive")
    if now < started_at:
        raise ValueError("now precedes started_at")
    elapsed = now - started_at
    return DurationResult(label, elapsed, budget, elapsed <= budget)


def summary_line(result: DurationResult) -> str:
    """Render one stable Markdown summary row."""
    verdict = "PASS" if result.passed else "FAIL"
    return (
        f"- `{result.label}` duration: {result.elapsed_seconds}s / "
        f"{result.budget_seconds}s budget — **{verdict}**\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--started-at-epoch", required=True, type=int)
    parser.add_argument("--budget-seconds", required=True, type=int)
    parser.add_argument("--now-epoch", type=int)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    now = args.now_epoch if args.now_epoch is not None else int(time.time())
    result = evaluate(
        args.label,
        started_at=args.started_at_epoch,
        now=now,
        budget=args.budget_seconds,
    )
    line = summary_line(result)
    print(line, end="")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write(line)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
