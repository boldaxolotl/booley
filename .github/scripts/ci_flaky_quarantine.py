#!/usr/bin/env python3
"""Fail on a bad entry of the flaky-test quarantine registry.

Usage: ``ci_flaky_quarantine.py``, from the checkout root with ``PYTHONPATH=src``
or Booley installed. Each entry needs an owner issue number, a test id pytest
collects, and to be listed once; then each owner must be an open ``flaky`` issue
whose title names the test id (read through ``gh api``). An empty registry runs
neither pytest nor ``gh``. See ``.github/ci/README.md``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from ci_flake_ledger import Gh, LedgerError, api_get, names_test, run_gh
from ci_pytest_rerun import load_quarantine, quarantine_entry

from booley.core.boundary import require_dict, require_list, require_str

_COLLECT_TIMEOUT_SECONDS = 300
_OUTPUT_TAIL_LINES = 15


def _entry_id(entry: object) -> str | None:
    match entry:
        case (str(test_id), *_):
            return test_id
    return None


def _collected(modules: list[str], root: Path) -> tuple[set[str], str | None]:
    """The node ids pytest collects from *modules*, or why it could not collect."""
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    try:
        result = subprocess.run(
            [*command, *modules],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COLLECT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return set(), f"pytest collection did not finish: {error}"
    if result.returncode not in (0, 5):  # 5: the modules hold no test
        output = f"{result.stdout}\n{result.stderr}".strip().splitlines()
        tail = "\n".join(output[-_OUTPUT_TAIL_LINES:])
        return set(), f"pytest collection exited {result.returncode}:\n{tail}"
    return set(result.stdout.splitlines()), None


def _unknown_ids(ids: list[str], root: Path) -> list[str]:
    modules = {test_id: test_id.split("::", 1)[0] for test_id in ids}
    present = sorted({module for module in modules.values() if (root / module).is_file()})
    collected, failure = _collected(present, root) if present else (set[str](), None)
    if failure is not None:
        return [failure]
    return [f"{test_id}: no such test" for test_id in modules if test_id not in collected]


def problems(entries: tuple[object, ...], root: Path) -> list[str]:
    """One line per bad entry, from the checkout alone."""
    found = [
        f"{_entry_id(entry) or repr(entry)}: no owner issue"
        for entry in entries
        if quarantine_entry(entry) is None
    ]
    ids = [test_id for entry in entries if (test_id := _entry_id(entry)) is not None]
    found += _unknown_ids(ids, root)
    found += [f"{test_id}: listed twice" for test_id, count in Counter(ids).items() if count > 1]
    return found


def _owner_defect(issue: dict[str, object], test_id: str) -> str | None:
    """Why *issue*, an API answer, cannot own *test_id*."""
    labels = [require_dict(label).get("name") for label in require_list(issue.get("labels"))]
    if "pull_request" in issue:
        return "is a pull request"
    if issue.get("state") != "open":
        return "is closed"
    if "flaky" not in labels:
        return "lacks the flaky label"
    if not names_test(require_str(issue, "title"), test_id):
        return "has a title that does not name the test"
    return None


def _owner_problem(test_id: str, number: int, gh: Gh) -> str | None:
    owner = f"{test_id}: owner issue #{number}"
    try:
        data = api_get(gh, owner, f"repos/{{owner}}/{{repo}}/issues/{number}")
        defect = _owner_defect(require_dict(json.loads(data), field="issue"), test_id)
    except LedgerError as error:
        return str(error)
    except ValueError as error:
        return f"{owner}: unreadable answer: {error}"
    return None if defect is None else f"{owner} {defect}"


def issue_problems(entries: tuple[object, ...], gh: Gh) -> list[str]:
    """One line per entry whose owner issue cannot own it."""
    owned = [parsed for entry in entries if (parsed := quarantine_entry(entry)) is not None]
    checked = [_owner_problem(test_id, number, gh) for test_id, number in owned]
    return [problem for problem in checked if problem is not None]


def main(argv: list[str] | None = None, *, gh: Gh = run_gh) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    root = Path.cwd()
    entries = load_quarantine(root)
    found = problems(entries, root) or issue_problems(entries, gh)
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
