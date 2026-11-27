"""The flake ledger rebuilt from the ``flaky-*`` artifacts of recent Tests runs."""

from __future__ import annotations

import io
import json
import sys
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.ci.gh_api_fake import GhApiFake
from tests.path_tool_fake import PathToolFake

from booley.runtime.timefmt import format_human_date

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))

_FIXTURES = Path(__file__).parent / "fixtures/flake_ledger"
_NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
_CLASS_ID = "tests/ci/test_flaky_sample.py::TestGroup::test_flaky"
_ERROR_ID = "tests/ci/test_flaky_sample.py::test_fixture_error"
_BOTH = [_CLASS_ID, _ERROR_ID]
_QUARANTINED_ID = "tests/ci/test_flaky_sample.py::test_always_fails"
_THREE = "3 hits within 7 days"


def _zip(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("junit.xml", data)
    return buffer.getvalue()


def _marker(name: str = "flaky-a", fixture: str = "flaky.xml") -> tuple[str, bytes, bool]:
    return name, _zip((_FIXTURES / fixture).read_bytes()), False


def _job(pytest_step: str, publish: str) -> dict[str, Any]:
    # The skipped step is another mode of the test job, between pytest and publish.
    steps = [("Run tests", pytest_step), ("Run other mode", "skipped")]
    steps.append(("Publish pytest evidence", publish))
    return {"name": "test / x", "steps": [{"name": n, "conclusion": c} for n, c in steps]}


def _main(fake: GhApiFake, *args: str) -> int:
    import ci_flake_ledger

    return ci_flake_ledger.main(list(args), gh=fake, now=lambda: _NOW)


@pytest.fixture(autouse=True)
def _checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "tests/ci/test_flaky_sample.py"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"")
    monkeypatch.chdir(tmp_path)


def test_hits_count_runs_not_jobs_or_attempts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = GhApiFake()
    jobs = (_marker("flaky-b"), _marker(), _marker(), ("junit-a", b"", False))
    fake.add_run(1, "2026-11-21T08:00:00Z", artifacts=jobs)
    later = (_marker(), _marker("flaky-c", "failed_again.xml"))
    fake.add_run(2, "2026-11-22T08:00:00Z", "main", artifacts=later)
    fake.add_run(3, "2026-11-23T08:00:00Z", conclusion="skipped")

    assert _main(fake, "--json") == 0

    report = json.loads(capsys.readouterr().out)
    assert report["window"] == {
        "start": "2026-11-20T12:00:00Z",
        "end": "2026-11-27T12:00:00Z",
        "days": 7,
    }
    assert (report["runs_read"], report["min_hits"]) == (2, 2)
    assert report["quarantine_bar"] == {"min_hits": 3, "days": 7}
    assert [entry["test_id"] for entry in report["ledger"]] == _BOTH
    assert [entry["test_id"] for entry in report["candidates"]] == _BOTH
    candidate = report["candidates"][0]
    assert candidate["hits"][0] == {
        "run_id": 1,
        "url": "https://github.com/o/r/actions/runs/1",
        "created_at": "2026-11-21T08:00:00Z",
        "branch": "codex/x",
        "jobs": ["a", "b"],
        "message": "AssertionError: flaked",
    }
    assert [hit["run_id"] for hit in candidate["hits"]] == [1, 2]
    assert candidate["title"] == f"Flaky test: {_CLASS_ID}"
    assert "https://github.com/o/r/actions/runs/1 " in candidate["body"]
    assert "https://github.com/o/r/actions/runs/2 " in candidate["body"]
    downloads = [path for path in fake.calls if path.endswith("/zip")]
    assert len(downloads) == 5  # every flaky-* artifact, never junit-a (id 4)
    assert not any("/artifacts/4/" in path or path.endswith("/jobs") for path in fake.calls)

    files = sorted(tmp_path.rglob("*"))
    assert _main(GhApiFake(), "--json") == 0
    assert json.loads(capsys.readouterr().out)["ledger"] == []
    assert sorted(tmp_path.rglob("*")) == files

    assert _main(fake) == 0
    text = capsys.readouterr().out
    window = f"{format_human_date(datetime(2026, 11, 20, 12, tzinfo=UTC))} to "
    window += format_human_date(_NOW)
    assert text.startswith(
        f"Flake ledger {window} (7 days): 2 Tests runs read, 2 test ids, 2 candidates, 0 meet the quarantine bar\n"
    )
    assert f"\n{_CLASS_ID}: 2 hits (candidate)\n" in text


def _pair(first: str, second: str) -> Callable[[GhApiFake], None]:
    def arrange(fake: GhApiFake) -> None:
        fake.add_run(1, first, artifacts=(_marker(),))
        fake.add_run(2, second, artifacts=(_marker(),))

    return arrange


def _one(fake: GhApiFake) -> None:
    fake.add_run(1, "2026-11-26T00:00:00Z", artifacts=(_marker(),))


def _runs(*days: int, fixture: str = "flaky.xml") -> Callable[[GhApiFake], None]:
    def arrange(fake: GhApiFake) -> None:
        for day in days:
            marked = (_marker(fixture=fixture),)
            fake.add_run(day, f"2026-11-{day}T00:00:00Z", artifacts=marked)

    return arrange


def _hit_after_issue(fake: GhApiFake) -> None:
    for number in range(100, 200):  # the owner issue is on the second page
        fake.add_issue(number, "Flaky test: other", "2026-11-25T00:00:00Z")
    fake.add_issue(7, f"Flaky test: `{_CLASS_ID}`", "2026-11-25T00:00:00Z")
    title = f"Flaky test: {_ERROR_ID}"
    fake.add_issue(8, title, "2026-11-25T00:00:00Z", pull_request=True)
    _one(fake)


def _hit_before_issue(fake: GhApiFake) -> None:
    title = f"Flaky test: {_CLASS_ID}"
    fake.add_issue(7, title, "2026-11-27T00:00:00Z")
    fake.add_issue(8, title, "2026-11-25T00:00:00Z", state="closed")
    fake.add_issue(9, title, "2026-11-25T00:00:00Z", label="bug")
    fake.add_issue(10, f"{title}[1]", "2026-11-25T00:00:00Z")
    _one(fake)


def _second_page(fake: GhApiFake) -> None:
    for run_id in range(1, 102):
        marked = (_marker(),) if run_id in (1, 101) else ()
        fake.add_run(run_id, "2026-11-26T00:00:00Z", artifacts=marked)


def _timed_out(fake: GhApiFake) -> None:
    jobs = (_job("failure", "failure"),)
    fake.add_run(1, "2026-11-26T00:00:00Z", jobs=jobs, conclusion="failure")


_OUTSIDE = _pair("2026-11-20T11:59:59Z", "2026-11-26T00:00:00Z")


@pytest.mark.parametrize(
    ("arrange", "args", "listed", "candidates", "quarantine"),
    [
        (_pair("2026-11-20T12:00:00Z", "2026-11-27T11:59:59Z"), (), _BOTH, _BOTH, []),
        (_one, (), _BOTH, [], []),
        (_OUTSIDE, (), _BOTH, [], []),
        (_OUTSIDE, ("--days", "8"), _BOTH, _BOTH, []),
        (_second_page, (), _BOTH, _BOTH, []),
        (_timed_out, (), [], [], []),
        (_runs(25, 26, fixture="quarantined.xml"), (), [_QUARANTINED_ID], [_QUARANTINED_ID], []),
        (_runs(21, 23, 26), (), _BOTH, _BOTH, [(test, None, [_THREE]) for test in _BOTH]),
        (_runs(19, 23, 27), ("--days", "9"), _BOTH, _BOTH, []),
        (_hit_after_issue, (), _BOTH, [], [(_CLASS_ID, 7, ["hit after #7 was opened"])]),
        (_hit_before_issue, (), _BOTH, [], []),
    ],
    ids=[
        "both-edges",
        "one-hit",
        "outside-window",
        "wider-window",
        "page-two",
        "timed-out",
        "quarantined-hit",
        "three-in-a-week",
        "three-over-eight-days",
        "hit-after-issue",
        "hit-before-issue",
    ],
)
def test_ledger_reports_candidates_and_the_quarantine_bar(
    capsys: pytest.CaptureFixture[str],
    arrange: Callable[[GhApiFake], None],
    args: tuple[str, ...],
    listed: list[str],
    candidates: list[str],
    quarantine: list[tuple[str, int | None, list[str]]],
) -> None:
    fake = GhApiFake()
    arrange(fake)

    assert _main(fake, "--json", *args) == 0

    report = json.loads(capsys.readouterr().out)
    assert [entry["test_id"] for entry in report["ledger"]] == listed
    assert [entry["test_id"] for entry in report["candidates"]] == candidates
    met = [(row["test_id"], row["issue"], row["reasons"]) for row in report["quarantine"]]
    assert met == quarantine

    assert _main(fake, *args) == 0
    text = capsys.readouterr().out
    assert f", {len(quarantine)} meet the quarantine bar" in text
    for test, _, _ in quarantine:
        mark = "candidate, quarantine bar" if test in candidates else "quarantine bar"
        assert f"hits ({mark})" in text.split(f"\n{test}: ")[1].splitlines()[0]


def test_a_deleted_module_keeps_its_junit_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tests/ci/test_flaky_sample.py").unlink()
    fake = GhApiFake()
    _one(fake)

    assert _main(fake, "--json") == 0

    assert [entry["test_id"] for entry in json.loads(capsys.readouterr().out)["ledger"]] == [
        "tests.ci.test_flaky_sample.TestGroup::test_flaky",
        "tests.ci.test_flaky_sample::test_fixture_error",
    ]


def _run_42(fake: GhApiFake, *artifacts: tuple[str, bytes, bool]) -> None:
    fake.add_run(42, "2026-11-26T00:00:00Z", artifacts=artifacts)


def _listing_fails(fake: GhApiFake) -> None:
    fake.fail("/runs", "error connecting to api.github.com")


def _artifacts_fail(fake: GhApiFake) -> None:
    _run_42(fake)
    fake.fail("/42/artifacts", "HTTP 502")


def _download_fails(fake: GhApiFake) -> None:
    _run_42(fake, _marker())
    fake.fail("/zip", "HTTP 404")


def _expired(fake: GhApiFake) -> None:
    _run_42(fake, ("flaky-a", b"", True))


def _not_a_zip(fake: GhApiFake) -> None:
    _run_42(fake, ("flaky-a", b"not a zip", False))


def _malformed(fake: GhApiFake) -> None:
    _run_42(fake, ("flaky-a", _zip(b"<testsuite"), False))


def _no_report(fake: GhApiFake) -> None:
    buffer = io.BytesIO()
    zipfile.ZipFile(buffer, "w").close()
    _run_42(fake, ("flaky-a", buffer.getvalue(), False))


def _too_many(fake: GhApiFake) -> None:
    fake.totals["workflow_runs"] = 1001


def _truncated(fake: GhApiFake) -> None:
    _run_42(fake)
    fake.totals["artifacts"] = 3


def _bad_response(fake: GhApiFake) -> None:
    _run_42(fake)
    fake.artifacts[42].append({"name": "flaky-a"})


def _issues_fail(fake: GhApiFake) -> None:
    fake.fail("/issues", "HTTP 403")


def _issues_endless(fake: GhApiFake) -> None:
    for number in range(1000):
        fake.add_issue(number, "Flaky test: other", "2026-11-25T00:00:00Z")


def _lost(
    publish: str = "failure", conclusion: str = "failure", attempt: int = 1
) -> Callable[[GhApiFake], None]:
    def arrange(fake: GhApiFake) -> None:
        jobs = (_job("success", publish),)
        fake.add_run(42, "2026-11-26T00:00:00Z", jobs=jobs, conclusion=conclusion, attempt=attempt)

    return arrange


_UNREADABLE = "run 42: artifact flaky-a unreadable: "
_LOST = "run 42: job test / x ran pytest but its evidence upload failed"


@pytest.mark.parametrize(
    ("arrange", "fragments"),
    [
        (
            _listing_fails,
            ["Tests runs 2026-11-20T12:00:00Z..", "error connecting to api.github.com"],
        ),
        (_artifacts_fail, ["run 42: ", "HTTP 502"]),
        (_expired, ["run 42: artifact flaky-a expired"]),
        (_download_fails, ["run 42: artifact flaky-a: ", "HTTP 404"]),
        (_not_a_zip, [_UNREADABLE]),
        (_malformed, [_UNREADABLE]),
        (_no_report, [_UNREADABLE + "no XML report"]),
        (_too_many, ["Tests runs ", "more than 1000"]),
        (_truncated, ["run 42: listed 0 of 3 artifacts"]),
        (_bad_response, ["run 42: unreadable artifacts response"]),
        (_issues_fail, ["flaky issues: ", "HTTP 403"]),
        (_issues_endless, ["flaky issues: ", "1000 or more"]),
        pytest.param(_lost(), [_LOST], id="lost-upload"),
        pytest.param(_lost("cancelled", "cancelled"), [_LOST], id="cancelled-upload"),
        pytest.param(_lost(conclusion="success", attempt=2), [_LOST], id="earlier-attempt"),
    ],
    ids=lambda value: value.__name__.strip("_") if callable(value) else "message",
)
def test_failures_name_the_run(
    capsys: pytest.CaptureFixture[str],
    arrange: Callable[[GhApiFake], None],
    fragments: list[str],
) -> None:
    fake = GhApiFake()
    arrange(fake)

    assert _main(fake, "--json") == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("flake ledger failed: ")
    assert [fragment for fragment in fragments if fragment not in output.err] == []


@pytest.mark.parametrize("installed", [True, False], ids=["unreachable", "no-gh"])
def test_unreachable_gh_fails_through_the_real_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installed: bool,
) -> None:
    import ci_flake_ledger

    if installed:
        gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
        gh.answer("api", returncode=1, stderr="error connecting to api.github.com")
    else:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert ci_flake_ledger.main([]) == 1

    error = capsys.readouterr().err
    assert error.startswith("flake ledger failed: Tests runs ")
    assert ("error connecting to api.github.com" if installed else "gh did not run") in error
