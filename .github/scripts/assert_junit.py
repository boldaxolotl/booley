#!/usr/bin/env python3
"""Summarize pytest JUnit evidence and enforce required-suite execution."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class Counts:
    tests: int
    failures: int
    errors: int
    skipped: int


def read_counts(path: Path) -> Counts:
    """Read aggregate counts from a pytest JUnit XML document."""
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError(f"{path} contains no testsuite")
    return Counts(
        tests=sum(int(suite.attrib.get("tests", 0)) for suite in suites),
        failures=sum(int(suite.attrib.get("failures", 0)) for suite in suites),
        errors=sum(int(suite.attrib.get("errors", 0)) for suite in suites),
        skipped=sum(int(suite.attrib.get("skipped", 0)) for suite in suites),
    )


def summary_line(label: str, counts: Counts) -> str:
    """Render one stable Markdown summary row."""
    return (
        f"- {label}: {counts.tests} collected, {counts.skipped} skipped, "
        f"{counts.failures} failed, {counts.errors} errors\n"
    )


def validate(counts: Counts, *, min_tests: int, max_skips: int | None) -> None:
    """Fail when a required suite silently collected too little or skipped too much."""
    if counts.tests < min_tests:
        raise ValueError(f"expected at least {min_tests} tests, collected {counts.tests}")
    if max_skips is not None and counts.skipped > max_skips:
        raise ValueError(f"expected at most {max_skips} skips, observed {counts.skipped}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("junit", type=Path)
    parser.add_argument("--label", default="pytest")
    parser.add_argument("--min-tests", type=int, default=1)
    parser.add_argument("--max-skips", type=int)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    counts = read_counts(args.junit)
    line = summary_line(args.label, counts)
    print(line, end="")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write(line)
    validate(counts, min_tests=args.min_tests, max_skips=args.max_skips)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
