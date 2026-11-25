"""The Dashboard session line for Reviewed Commits (#5456).

Real temporary Git repositories and the production Review Run writer feed the reader; the
app tests drive ``DashboardApp`` with a fixed snapshot.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard import model, sessions
from booley.harness.dashboard.app import DashboardApp
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.review.review_run import store_review_run
from tests.golden.conftest import assert_matches_golden
from tests.harness.test_dashboard_measurement import (
    change,
    declared_repository,
    measure_job,
    reader,
    short,
)
from tests.harness.test_dashboard_preview import move_to, session_row, snapshot_of
from tests.runtime.test_commit_export import git, write
from tests.time_budget import hang_guard_s

FLAGGED = '[project]\n\n[features]\nenable = ["reviewed-commits"]\n'


def review():
    """The reviewed-commit module, imported when a test runs so a missing module fails that test."""
    return importlib.import_module("booley.harness.dashboard.reviewed_commit")


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", hang_guard_s())
    monkeypatch.delenv("BOOLEY_DIR", raising=False)


def flag_on(repo: Path) -> None:
    write(repo / ".booley" / "booley.toml", FLAGGED)


def review_run(
    repo: Path, commit: str, run_id: str, outcome: str, *, kind: str = "discovery"
) -> Path:
    """Store one minimal schema-1 Review Run with its Markdown report; returns the JSON path."""
    document: dict[str, Any] = {
        "schema_version": 1,
        "kind": kind,
        "run_id": run_id,
        "commit": commit,
        "outcome": outcome,
    }
    return store_review_run(repo / ".booley", document, f"# {run_id}\n")


# --- AC1 ---------------------------------------------------------------------


def test_the_reader_shows_the_newest_reviewed_commit_with_its_latest_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, before = declared_repository(tmp_path / "repo")
    flag_on(repo)
    review_run(repo, before, "20260101T000000Z", "pass")  # past the merge-base: never reached
    merge_base = change(repo, "module m; endmodule\n", "merge-base")
    review_run(repo, merge_base, "20260102T000000Z", "pass")
    newer = review_run(repo, merge_base, "20260103T000000Z", "open")
    git(repo, "switch", "-q", "-c", "feature")
    head = change(repo, "module f; endmodule\n", "feature")
    review_run(repo, head, "20260104T000000Z", "open")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(model, "namespace", lambda: "pid:[17]")
    SessionRegistry(state).upsert(
        Attribution("codex:00", "thread", str(repo), "repo:w", namespace="pid:[17]"),
        "sim",
        "completed",
        now=time.time(),
    )

    def read() -> Any:
        return model.DashboardReader(state, state).read().reviews.get(str(repo))

    assert read() == review().ReviewLine(
        f"Reviewed {short(merge_base)}", str(newer.with_suffix(".md"))
    )
    passing = review_run(repo, head, "20260105T000000Z", "pass")
    assert read() == review().ReviewLine(
        f"Reviewed {short(head)}", str(passing.with_suffix(".md"))
    )
    passing.with_suffix(".md").unlink()
    assert read() == review().ReviewLine(f"Reviewed {short(head)}", None)


@pytest.mark.parametrize("case", ["flag-off", "no-runs", "open-only", "damaged", "unreadable"])
def test_no_review_part_without_a_reviewed_commit_or_the_flag(tmp_path: Path, case: str) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    if case != "flag-off":
        flag_on(repo)
    if case in {"flag-off", "unreadable"}:
        review_run(repo, head, "20260101T000000Z", "pass")
    if case == "open-only":
        review_run(repo, head, "20260101T000000Z", "open")
    if case == "damaged":
        write(repo / ".booley" / "reviews" / head / "r.json", json.dumps({"schema_version": 1}))
    if case == "unreadable":
        write(repo / ".booley" / "reviews" / head / "r.json", "{not json")

    assert review().review_lines({str(repo), ""}) == {}


def test_a_checkout_with_an_invalid_booley_toml_or_without_commits_has_no_part(
    tmp_path: Path,
) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    review_run(repo, head, "20260101T000000Z", "pass")
    write(repo / ".booley" / "booley.toml", '[features]\nenable = ["no-such-flag"]\n')
    empty = tmp_path / "no-commits"
    write(empty / ".booley" / "booley.toml", FLAGGED)
    git(empty, "init", "-q", "-b", "main")

    assert review().review_lines({str(repo), str(empty)}) == {}


def lined(
    *, report: str | None = "/store/run.md", review_report: str | None = "/store/review.md"
) -> model.DashboardSnapshot:
    rows = [session_row(0, "wt0", work_dir="/work/a", branch="main")]
    measurements = (
        {} if report is None else {"/work/a": reader().MeasurementLine("Measured 1a2b3c4", report)}
    )
    reviews = {"/work/a": review().ReviewLine("Reviewed 5d6e7f8", review_report)}
    return dataclasses.replace(snapshot_of(rows), measurements=measurements, reviews=reviews)


@pytest.mark.asyncio
async def test_enter_and_the_preview_open_each_report_of_the_line() -> None:
    opened: list[str] = []
    key = sessions.MEASUREMENT_PREFIX + "codex:00"
    both = lined()
    app = DashboardApp(lambda: both, lambda path: opened.append(path) or "")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.row_keys[-1] == key
        assert [row[0].plain for row in app._overview()[2]][-1].strip() == (
            "Measured 1a2b3c4 · Reviewed 5d6e7f8"
        )
        move_to(app, key)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert opened == ["/store/run.md", "/store/review.md"]
    async with DashboardApp(lambda: both, lambda path: "").run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        detail = str(pilot.app.query_one("#detail").render())
        assert detail.endswith("Reports:\n/store/run.md\n/store/review.md")
        spans = pilot.app.query_one("#detail").render().spans  # type: ignore[attr-defined]
        clicks = {
            str(span.style.meta["@click"])
            for span in spans
            if not isinstance(span.style, str) and span.style.meta
        }
        assert clicks == {"app.open_path('/store/run.md')", "app.open_path('/store/review.md')"}
    opened.clear()
    only = lined(report=None)
    alone = DashboardApp(lambda: only, lambda path: opened.append(path) or "")
    async with alone.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        move_to(alone, key)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert opened == ["/store/review.md"]
    bare = lined(report=None, review_report=None)
    unlinked = DashboardApp(lambda: bare)
    async with unlinked.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert key not in unlinked.row_keys  # a report-less review part is no selectable line


@pytest.mark.asyncio
async def test_a_session_without_reports_has_no_reports_section() -> None:
    plain = snapshot_of([session_row(0, "wt0", work_dir="/work/a", branch="main")])
    async with DashboardApp(lambda: plain).run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        assert "Reports:" not in str(pilot.app.query_one("#detail").render())


def test_session_lines_match_their_golden_file() -> None:
    rows = [session_row(n, f"wt{n}", work_dir=f"/work/{n}", branch="main") for n in range(4)]
    line = reader().MeasurementLine
    part = review().ReviewLine
    measuring = line(
        "Measured 1a2b3c4",
        "/store/run.md",
        measure_job("/work/3", progress={"finished": 2, "total": 5}),
    )
    snapshot = dataclasses.replace(
        snapshot_of(rows),
        measurements={
            "/work/0": line("Measured 1a2b3c4", "/store/run.md"),
            "/work/2": line("last Measured 1a2b3c4 · latest 9f8e7d6: 1 unmet", None),
            "/work/3": measuring,
        },
        reviews={
            "/work/0": part("Reviewed 5d6e7f8", "/store/review.md"),
            "/work/1": part("Reviewed 5d6e7f8", "/store/review.md"),
            "/work/3": part("Reviewed 5d6e7f8", None),
        },
    )
    app = DashboardApp(lambda: snapshot)
    app.snapshot = snapshot
    text = "\n".join(re.sub(r"\s+$", "", row[0].plain) for row in app._overview()[2])

    assert_matches_golden("dashboard/session-lines.txt", text)
