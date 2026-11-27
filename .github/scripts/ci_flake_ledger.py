#!/usr/bin/env python3
"""List the tests that flaked or failed while quarantined in recent ``Tests`` runs.

Usage: ``ci_flake_ledger.py [--days N] [--json]``, from a repository checkout with
``PYTHONPATH=src``. Reads the ``flaky-*`` artifacts and the open ``flaky`` issues
through ``gh api`` (``GH_REPO`` or the checkout names the repository) and writes no
file. Exit 1 prints no ledger and names the run or listing that could not be read;
see ``.github/ci/README.md``.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import shutil
import subprocess
import sys
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ci_pytest_rerun import HIT_TAGS, RerunRefusedError, node_id

from booley.core.boundary import (
    parse_positive_int_arg,
    require_dict,
    require_int,
    require_list,
    require_str,
)
from booley.runtime.timefmt import format_human_date, parse_timestamp, rfc3339_from_datetime

MIN_HITS = 2
#: The bar for quarantine: this many hits within this many days, or a hit after
#: the test's open ``flaky`` issue was opened.
QUARANTINE_HITS = 3
QUARANTINE_DAYS = 7
MARKER_PREFIX = "flaky-"
_RUNS = "repos/{owner}/{repo}/actions/workflows/test.yml/runs"
_ACTIONS = "repos/{owner}/{repo}/actions"
_ISSUES = "repos/{owner}/{repo}/issues"
_PAGE_SIZE = 100
_MAX_ITEMS = 1000  # GitHub returns no more than this for a filtered run listing
_GH_TIMEOUT_SECONDS = 120
_PUBLISH_STEP = "Publish pytest evidence"
_LOST_UPLOAD = ("failure", "cancelled")

Gh = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]


class LedgerError(Exception):
    """A run or listing could not be read, so no ledger is reported."""


@dataclass(frozen=True)
class Run:
    id: int
    url: str
    created_at: datetime
    branch: str
    conclusion: str
    attempt: int


@dataclass(frozen=True)
class Hit:
    run: Run
    jobs: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Entry:
    test_id: str
    hits: tuple[Hit, ...]


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    created_at: datetime


@dataclass(frozen=True)
class Qualified:
    """A test id that meets the quarantine bar."""

    test_id: str
    issue: int | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    start: datetime
    end: datetime
    days: int
    runs_read: int
    ledger: tuple[Entry, ...]
    quarantine: tuple[Qualified, ...]


def run_gh(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [shutil.which("gh") or "gh", *args],
        capture_output=True,
        check=False,
        timeout=_GH_TIMEOUT_SECONDS,
    )


def api_get(gh: Gh, where: str, path: str, **fields: str) -> bytes:
    args = ["api", "--method", "GET", path]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    try:
        result = gh(args)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LedgerError(f"{where}: gh did not run: {error}") from error
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise LedgerError(f"{where}: gh api {path} failed: {stderr}")
    return result.stdout


@contextmanager
def _reading(where: str, what: str) -> Iterator[None]:
    try:
        yield
    except ValueError as error:
        raise LedgerError(f"{where}: unreadable {what} response: {error}") from error


def _pages(gh: Gh, where: str, path: str, key: str, **fields: str) -> list[dict[str, Any]]:
    """Read every item of a listing, or fail: a short listing is not a complete one."""
    items: list[dict[str, Any]] = []
    total = 0
    page, pages = 1, 1
    while page <= pages:
        data = api_get(gh, where, path, per_page=str(_PAGE_SIZE), page=str(page), **fields)
        with _reading(where, key):
            body = require_dict(json.loads(data), field="response")
            if page == 1:
                total = require_int(body.get("total_count"), field="total_count")
                if total > _MAX_ITEMS:
                    raise LedgerError(f"{where}: {path} lists more than {_MAX_ITEMS} {key}")
                pages = math.ceil(total / _PAGE_SIZE)
            items += [require_dict(item, field=key) for item in require_list(body.get(key))]
        page += 1
    if len(items) != total:
        raise LedgerError(f"{where}: listed {len(items)} of {total} {key}")
    return items


def _run(item: dict[str, Any]) -> Run:
    return Run(
        require_int(item.get("id"), field="id"),
        require_str(item, "html_url"),
        parse_timestamp(require_str(item, "created_at")),
        str(item.get("head_branch") or ""),
        str(item.get("conclusion") or ""),
        require_int(item.get("run_attempt"), field="run_attempt"),
    )


def tests_runs(gh: Gh, start: datetime, end: datetime) -> list[Run]:
    """The completed, not skipped ``Tests`` runs created in [start, end)."""
    where = f"Tests runs {rfc3339_from_datetime(start)}..{rfc3339_from_datetime(end)}"
    runs: dict[int, Run] = {}
    day = start.date()
    # One day at a time: a filtered listing stops at _MAX_ITEMS runs.
    while day <= end.date():
        created = f"{day.isoformat()}..{day.isoformat()}"
        items = _pages(gh, where, _RUNS, "workflow_runs", status="completed", created=created)
        with _reading(where, "workflow_runs"):
            for run in map(_run, items):
                if start <= run.created_at < end and run.conclusion != "skipped":
                    runs[run.id] = run
        day += timedelta(days=1)
    return list(runs.values())


def flaky_issues(gh: Gh) -> list[Issue]:
    """The open ``flaky`` issues; the listing has no total, so a short page ends it."""
    where = "flaky issues"
    issues: list[Issue] = []
    for page in range(1, _MAX_ITEMS // _PAGE_SIZE + 1):
        fields = {"labels": "flaky", "state": "open", "per_page": str(_PAGE_SIZE)}
        data = api_get(gh, where, _ISSUES, page=str(page), **fields)
        with _reading(where, "issues"):
            items = [require_dict(item, field="issue") for item in require_list(json.loads(data))]
            issues += [
                Issue(
                    require_int(item.get("number"), field="number"),
                    require_str(item, "title"),
                    parse_timestamp(require_str(item, "created_at")),
                )
                for item in items
                if "pull_request" not in item
            ]
        if len(items) < _PAGE_SIZE:
            return issues
    raise LedgerError(f"{where}: {_MAX_ITEMS} or more are open")


def names_test(title: str, test_id: str) -> bool:
    """True when *test_id* stands as a word of an issue *title*."""
    # A word ends at white space or a backtick; punctuation around it is not part of it.
    edge, punctuation = r"[\s`]", r"[:,;()]*"
    pattern = rf"(?:^|{edge}){punctuation}{re.escape(test_id)}{punctuation}(?:$|{edge})"
    return re.search(pattern, title) is not None


def quarantine_reasons(entry: Entry, issue: Issue | None) -> list[str]:
    """Why *entry* meets the quarantine bar; empty when it does not."""
    times = [hit.run.created_at for hit in entry.hits]  # oldest first
    span = timedelta(days=QUARANTINE_DAYS)
    reasons: list[str] = []
    runs_of_hits = zip(times, times[QUARANTINE_HITS - 1 :], strict=False)
    if any(last - first < span for first, last in runs_of_hits):
        reasons.append(f"{QUARANTINE_HITS} hits within {QUARANTINE_DAYS} days")
    if issue is not None and any(time > issue.created_at for time in times):
        reasons.append(f"hit after #{issue.number} was opened")
    return reasons


def _qualified(ledger: tuple[Entry, ...], issues: list[Issue]) -> tuple[Qualified, ...]:
    found: list[Qualified] = []
    for entry in ledger:
        owners = [issue for issue in issues if names_test(issue.title, entry.test_id)]
        owner = min(owners, key=lambda issue: issue.created_at, default=None)
        reasons = quarantine_reasons(entry, owner)
        if reasons:
            number = None if owner is None else owner.number
            found.append(Qualified(entry.test_id, number, tuple(reasons)))
    return tuple(found)


def _flaky_cases(data: bytes, where: str) -> list[tuple[str, str, str]]:
    """The (classname, name, first message line) of each hit in one artifact."""
    cases: list[tuple[str, str, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            reports = [name for name in archive.namelist() if name.endswith(".xml")]
            if not reports:
                raise LedgerError(f"{where} unreadable: no XML report")
            roots = [ElementTree.fromstring(archive.read(name)) for name in reports]
    except (zipfile.BadZipFile, zlib.error, ElementTree.ParseError) as error:
        raise LedgerError(f"{where} unreadable: {error}") from error
    for case in (case for root in roots for case in root.iter("testcase")):
        flaky = [child for child in case if child.tag in HIT_TAGS]
        if flaky:
            lines = flaky[0].get("message", "").splitlines() or [""]
            cases.append((case.get("classname", ""), case.get("name", ""), lines[0]))
    return cases


def _test_id(classname: str, name: str, root: Path) -> str:
    try:
        return node_id(classname, name, root)
    except RerunRefusedError:
        # The module was deleted or moved since the run; its hits still group.
        return f"{classname}::{name}"


def run_hits(gh: Gh, run: Run, root: Path) -> dict[str, Hit]:
    """One hit per test id that has one in any job or attempt of *run*."""
    where = f"run {run.id}"
    items = _pages(gh, where, f"{_ACTIONS}/runs/{run.id}/artifacts", "artifacts")
    with _reading(where, "artifacts"):
        markers = sorted(
            (name, require_int(item.get("id"), field="id"), item.get("expired") is True)
            for item in items
            if (name := require_str(item, "name")).startswith(MARKER_PREFIX)
        )
    jobs: dict[str, set[str]] = {}
    messages: dict[str, str] = {}
    for name, artifact_id, expired in markers:
        artifact = f"{where}: artifact {name}"
        if expired:
            raise LedgerError(f"{artifact} expired")
        data = api_get(gh, artifact, f"{_ACTIONS}/artifacts/{artifact_id}/zip")
        for classname, test, message in _flaky_cases(data, artifact):
            test_id = _test_id(classname, test, root)
            jobs.setdefault(test_id, set()).add(name.removeprefix(MARKER_PREFIX))
            messages.setdefault(test_id, message)
    return {key: Hit(run, tuple(sorted(jobs[key])), messages[key]) for key in jobs}


def _check_published(gh: Gh, run: Run) -> None:
    """Fail when a job ran its tests but lost the evidence, which may hold a marker."""
    where = f"run {run.id}"
    path = f"{_ACTIONS}/runs/{run.id}/jobs"
    items = _pages(gh, where, path, "jobs", filter="all")
    with _reading(where, "jobs"):
        for job in items:
            steps = [require_dict(step) for step in require_list(job.get("steps", []))]
            # The composite action is one step, so the step that ran before it ran pytest.
            # The test job's unused modes are skipped steps between the two.
            ran = [step for step in steps if step.get("conclusion") != "skipped"]
            for tests, publish in pairwise(ran):
                if (
                    publish.get("name") == _PUBLISH_STEP
                    and publish.get("conclusion") in _LOST_UPLOAD
                    and tests.get("conclusion") == "success"
                ):
                    raise LedgerError(
                        f"{where}: job {job.get('name')} ran pytest but its evidence upload "
                        "failed or was cancelled"
                    )


def build(hits_per_run: list[dict[str, Hit]]) -> tuple[Entry, ...]:
    """Group hits by test id, most hit first."""
    found: dict[str, list[Hit]] = {}
    for hits in hits_per_run:
        for test_id, hit in hits.items():
            found.setdefault(test_id, []).append(hit)
    entries = [
        Entry(test_id, tuple(sorted(hits, key=lambda hit: (hit.run.created_at, hit.run.id))))
        for test_id, hits in found.items()
    ]
    return tuple(sorted(entries, key=lambda entry: (-len(entry.hits), entry.test_id)))


def collect(gh: Gh, start: datetime, end: datetime, root: Path) -> Report:
    runs = tests_runs(gh, start, end)
    per_run: list[dict[str, Hit]] = []
    for run in runs:
        per_run.append(run_hits(gh, run, root))
        # A failed upload fails its job, so only a successful first attempt lost nothing.
        if run.conclusion != "success" or run.attempt > 1:
            _check_published(gh, run)
    ledger = build(per_run)
    quarantine = _qualified(ledger, flaky_issues(gh))
    return Report(start, end, (end - start).days, len(runs), ledger, quarantine)


def _hit_json(hit: Hit) -> dict[str, object]:
    return {
        "run_id": hit.run.id,
        "url": hit.run.url,
        "created_at": rfc3339_from_datetime(hit.run.created_at),
        "branch": hit.run.branch,
        "jobs": list(hit.jobs),
        "message": hit.message,
    }


def _draft(entry: Entry, report: Report) -> tuple[str, str]:
    """The title and body of the ``flaky`` issue the issue pipeline may file."""
    lines = [
        f"`{entry.test_id}` failed then passed on rerun, or failed while quarantined, "
        f"in {len(entry.hits)} `Tests` runs "
        f"between {format_human_date(report.start)} and {format_human_date(report.end)}.",
        "",
    ]
    for hit in entry.hits:
        # In a code span, an `@name` from a test message notifies nobody.
        message = hit.message.replace("`", "'")
        lines.append(
            f"- {hit.run.url} ({format_human_date(hit.run.created_at)}, "
            f"branch `{hit.run.branch}`, jobs `{', '.join(hit.jobs)}`): `{message}`"
        )
    lines += [
        "",
        "Found by `.github/scripts/ci_flake_ledger.py`; see the Flake ledger section "
        "of `.github/ci/README.md`.",
    ]
    return f"Flaky test: {entry.test_id}", "\n".join(lines) + "\n"


def report_json(report: Report) -> str:
    ledger: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    for entry in report.ledger:
        row: dict[str, object] = {
            "test_id": entry.test_id,
            "hits": [_hit_json(hit) for hit in entry.hits],
        }
        ledger.append(row)
        if len(entry.hits) >= MIN_HITS:
            title, body = _draft(entry, report)
            candidates.append({**row, "title": title, "body": body})
    window = {
        "start": rfc3339_from_datetime(report.start),
        "end": rfc3339_from_datetime(report.end),
        "days": report.days,
    }
    document = {
        "window": window,
        "min_hits": MIN_HITS,
        "runs_read": report.runs_read,
        "ledger": ledger,
        "candidates": candidates,
        "quarantine_bar": {"min_hits": QUARANTINE_HITS, "days": QUARANTINE_DAYS},
        "quarantine": [
            {"test_id": item.test_id, "issue": item.issue, "reasons": list(item.reasons)}
            for item in report.quarantine
        ],
    }
    return json.dumps(document, indent=2, sort_keys=True)


def render_text(report: Report) -> str:
    candidates = sum(len(entry.hits) >= MIN_HITS for entry in report.ledger)
    lines = [
        f"Flake ledger {format_human_date(report.start)} to "
        f"{format_human_date(report.end)} ({report.days} days): "
        f"{report.runs_read} Tests runs read, {len(report.ledger)} test ids, "
        f"{candidates} candidates, {len(report.quarantine)} meet the quarantine bar"
    ]
    meets = {item.test_id for item in report.quarantine}
    for entry in report.ledger:
        flags = (("candidate", len(entry.hits) >= MIN_HITS),)
        flags += (("quarantine bar", entry.test_id in meets),)
        marks = ", ".join(label for label, on in flags if on)
        mark = f" ({marks})" if marks else ""
        lines.append(f"{entry.test_id}: {len(entry.hits)} hits{mark}")
        lines += [
            f"  run {hit.run.id} {format_human_date(hit.run.created_at)} "
            f"{hit.run.branch} [{', '.join(hit.jobs)}] {hit.run.url}: {hit.message}"
            for hit in entry.hits
        ]
    return "\n".join(lines)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def main(
    argv: list[str] | None = None, *, gh: Gh = run_gh, now: Callable[[], datetime] = _now
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=parse_positive_int_arg, default=7)
    parser.add_argument("--json", action="store_true", help="print the JSON document only")
    args = parser.parse_args(argv)
    end = now()
    try:
        report = collect(gh, end - timedelta(days=args.days), end, Path.cwd())
    except LedgerError as error:
        print(f"flake ledger failed: {error}", file=sys.stderr)
        return 1
    print(report_json(report) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
