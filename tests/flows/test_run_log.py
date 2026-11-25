"""Tests for the Flow-neutral durable run-log interface."""

import errno
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from booley.flows import run_log
from tests.conftest import symlink_or_skip
from tests.fusesoc_test_support import symlinked_booley_dir
from tests.sharing_error_support import flaky, hold_opens, hold_replaces, no_retry_delay

# ---------------------------------------------------------------------------
# write_run_log
# ---------------------------------------------------------------------------


class TestWriteRunLog:
    """run.log — the durable raw-output copy that survives stdout truncation."""

    @pytest.mark.parametrize(
        ("writes", "max_bytes", "expected"),
        [
            (("hello\nworld\n",), 4000, "hello\nworld\n"),
            (("line\n" * 100,), 10_000, "line\n" * 100),
            (("complete simulator evidence\n",), None, "complete simulator evidence\n"),
            (("first run", "second run"), 4000, "second run"),
        ],
        ids=["short", "under-cap", "no-cap", "overwrite"],
    )
    def test_written_verbatim(self, tmp_path, writes, max_bytes, expected):
        for text in writes:
            path = run_log.write_run_log(tmp_path, text, max_bytes=max_bytes)
        # CONTRACT: the simulate summary prints <work_dir>/run.log — the
        # filename must not drift.
        assert path == tmp_path / "run.log"
        assert path.read_text(encoding="utf-8") == expected

    def test_over_cap_keeps_tail_with_marker(self, tmp_path):
        lines = [f"line {i:06d}" for i in range(500)]
        text = "\n".join(lines) + "\n"
        cap = 2_000
        run_log.write_run_log(tmp_path, text, max_bytes=cap)
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= cap  # the written file never exceeds the cap
        content = raw.decode("utf-8")
        marker, body = content.split("\n", 1)
        # One-line marker records the original (pre-cap) size.
        assert marker.startswith("[RUN_LOG TRUNCATED]")
        assert str(len(text.encode("utf-8"))) in marker
        # The TAIL survives (verdict/sentinel wording lives there) ...
        assert body.endswith("line 000499\n")
        # ... and starts on a clean line boundary, not mid-line.
        assert body.splitlines()[0].startswith("line ")

    def test_atomic_leaves_no_tmp_file(self, tmp_path):
        # Atomic tmp+rename: the PID-suffixed staging file must be gone.
        run_log.write_run_log(tmp_path, "output")
        assert (tmp_path / "run.log").exists()
        assert list(tmp_path.glob("run.log.*.tmp")) == []

    def test_undecodable_text_never_raises(self, tmp_path):
        # A lone surrogate (undecoded byte smuggled through errors="replace"
        # upstream) must not lose the whole log to an encode error.
        run_log.write_run_log(tmp_path, "before \udce9 after\n")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert "before" in content and "after" in content


# ---------------------------------------------------------------------------
# begin_run_log — the F-26 staleness guard
# ---------------------------------------------------------------------------


class TestRunLogHeader:
    """A run.log must never read as live progress while holding old bytes."""

    def test_begin_erases_the_previous_runs_output_and_names_the_run(self, tmp_path):
        run_log.write_run_log(tmp_path, "TEST PASSED\nold verdict\n")
        run_log.begin_run_log(tmp_path, flow="lint", target="lint_top", run="run-2")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert "TEST PASSED" not in content
        assert content.startswith(f"{run_log.RUN_LOG_HEADER_PREFIX} ")
        assert "run=run-2 flow=lint target=lint_top started=" in content
        assert run_log.RUN_LOG_PENDING in content
        first_line = content.splitlines()[0]
        assert first_line.endswith("Z")

    def test_write_preserves_the_header(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        run_log.write_run_log(tmp_path, "[SIM_RESULT] PASSED\n")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content.splitlines()[0].startswith(run_log.RUN_LOG_HEADER_PREFIX)
        assert run_log.RUN_LOG_PENDING not in content
        assert content.endswith("[SIM_RESULT] PASSED\n")
        assert "run=run-2 flow=sim target=sim_fifo" in content.splitlines()[0]

    def test_cap_accounts_for_the_preserved_header(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        cap = 500
        run_log.write_run_log(tmp_path, "line\n" * 500, max_bytes=cap)
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= cap
        assert raw.startswith(run_log.RUN_LOG_HEADER_PREFIX.encode())

    def test_run_token_prefers_the_job_run_id(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_RUN_ID", "simulate-20260726T120000-1")
        assert run_log.current_run_token() == "simulate-20260726T120000-1"
        monkeypatch.delenv("BOOLEY_RUN_ID")
        assert run_log.current_run_token().startswith("pid")


class TestRunLogProgress:
    """F-18: an in-flight run must be readable, without ever looking finished."""

    def test_progress_shows_a_live_tail_and_keeps_the_header(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        run_log.write_run_log_progress(
            tmp_path, "cycle 100\ncycle 200\n", elapsed_s=42.0, line_count=2, idle_s=3.0
        )
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content.splitlines()[0].startswith(run_log.RUN_LOG_HEADER_PREFIX)
        assert "42s elapsed, 2 output line(s), last output 3s ago" in content
        assert "cycle 200" in content

    def test_progress_never_reads_as_a_finished_run(self, tmp_path):
        # The whole point of the marker prefix: a live tail is not a verdict.
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        run_log.write_run_log_progress(tmp_path, "TEST SUCCEEDED\n", elapsed_s=5.0, line_count=1)
        in_progress = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert run_log.RUN_LOG_IN_PROGRESS_PREFIX in in_progress
        run_log.write_run_log(tmp_path, "TEST SUCCEEDED\n[SIM_RESULT] PASSED\n")
        finished = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert run_log.RUN_LOG_IN_PROGRESS_PREFIX not in finished

    def test_progress_keeps_the_tail_when_capped(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        body = "".join(f"line {i}\n" for i in range(2000))
        run_log.write_run_log_progress(
            tmp_path, body, elapsed_s=1.0, line_count=2000, max_bytes=400
        )
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= 400 + len(run_log.RUN_LOG_HEADER_PREFIX) + 200
        assert b"line 1999" in raw


def test_begin_run_log_never_truncates_unrelated_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    failures = []
    for kind in ("symlink", "hardlink", "ancestor"):
        case = kind
        case_root = tmp_path / case
        protected = case_root / "protected"
        protected.mkdir(parents=True)
        sentinel = protected / "run.log"
        sentinel.write_text("retained evidence")
        evidence = case_root / "evidence"
        if kind == "ancestor":
            symlink_or_skip(evidence, protected, target_is_directory=True)
        else:
            evidence.mkdir()
            if kind == "symlink":
                symlink_or_skip(evidence / "run.log", sentinel)
            else:
                (evidence / "run.log").hardlink_to(sentinel)
        with suppress(OSError):
            run_log.begin_run_log(evidence, flow="sim", target="sim", owner=case_root)
        if sentinel.read_text() != "retained evidence":
            failures.append(case)
    assert failures == [], f"retained evidence was truncated: {failures}"


def test_begin_run_log_refreshes_under_symlinked_parent(tmp_path):
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    symlink_or_skip(alias, physical, target_is_directory=True)
    work = alias / "work"
    work.mkdir()
    run_log.write_run_log(work, "previous run")
    run_log.begin_run_log(work, flow="lint", target="lint_top")
    assert "previous run" not in (work / "run.log").read_text(encoding="utf-8")
    assert run_log.RUN_LOG_PENDING in (work / "run.log").read_text(encoding="utf-8")


def test_identity_checks_only_through_owning_root(tmp_path):
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    symlink_or_skip(alias, physical, target_is_directory=True)
    owner = alias / "owner"
    work = owner / "work"
    work.mkdir(parents=True)
    run_log.write_run_log(work, "previous run")
    identity = run_log.RunLogIdentity.create(work, "sim", "attempt", (owner,))
    assert "previous run" not in identity.path.read_text(encoding="utf-8")
    assert identity.pointer(work, "attempt", owner)["live"] is True


@pytest.mark.parametrize("linked", ["entry", "below"])
def test_run_log_identity_follows_only_the_booley_dir_entry(tmp_path, linked):
    """#2191: the run log crosses the Booley Directory link, never a link below it."""
    project = tmp_path / "project"
    project.mkdir()
    entry = project / ".booley"
    symlinked_booley_dir(project, tmp_path / "external")
    if linked == "below":
        # A link below the entry (.runtime) stays refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink_or_skip(entry / ".runtime", outside, target_is_directory=True)
    work = entry / ".runtime" / "attempt"
    work.mkdir(parents=True, exist_ok=True)

    if linked == "below":
        with pytest.raises(OSError, match="symbolic link"):
            run_log.RunLogIdentity.create(work, "sim", "attempt", (project,), booley_dir=entry)
        return
    identity = run_log.RunLogIdentity.create(work, "sim", "attempt", (project,), booley_dir=entry)
    # Appending changes the fingerprint, so pointer() re-opens through the entry.
    with identity.path.open("a", encoding="utf-8") as stream:
        stream.write("output\n")
    assert identity.pointer(work, "attempt", project) is not None


def _started_log(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    run_log.begin_run_log(work, flow="lint", target="lint_top")
    return work, work / "run.log"


def _live_identity(tmp_path):
    """A sim run-log identity whose fingerprint has changed, so pointer() re-reads."""
    owner = tmp_path / "owner"
    work = owner / "work"
    work.mkdir(parents=True)
    identity = run_log.RunLogIdentity.create(work, "sim", "attempt", (owner,))
    with identity.path.open("a", encoding="utf-8") as stream:
        stream.write("output\n")
    return identity, work, owner


@pytest.mark.parametrize(("held", "raises"), [(1, False), (100, True)])
def test_run_log_write_retries_replace_sharing_error(tmp_path, monkeypatch, held, raises):
    work, log = _started_log(tmp_path)
    attempts = hold_replaces(monkeypatch, {log: held})
    if raises:
        with pytest.raises(PermissionError):
            run_log.write_run_log(work, "output\n")
        assert attempts[log] > 1
        return
    run_log.write_run_log(work, "output\n")
    assert attempts[log] == 2
    content = log.read_text(encoding="utf-8")
    assert content.startswith(run_log.RUN_LOG_HEADER_PREFIX)
    assert content.endswith("output\n")


def test_run_log_write_raises_lasting_header_read_sharing_error(tmp_path, monkeypatch):
    work, log = _started_log(tmp_path)
    hold_opens(monkeypatch, {log: 100})
    # The writer must not silently drop the ownership header either.
    with pytest.raises(PermissionError):
        run_log.write_run_log(work, "output\n")


def test_run_log_head_keeps_posix_permission_error_as_empty(tmp_path, monkeypatch):
    work, log = _started_log(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    real_open = Path.open

    def deny_run_log(path, *args, **kwargs):
        if path == log:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_run_log)
    run_log.write_run_log(work, "out\n")
    monkeypatch.undo()
    # An unreadable head is a headerless log: the output is written alone.
    assert log.read_text(encoding="utf-8") == "out\n"


@pytest.mark.parametrize("failures", [1, 100])
def test_run_log_pointer_retries_sharing_error(tmp_path, monkeypatch, failures):
    identity, work, owner = _live_identity(tmp_path)
    stale = identity.fingerprint
    no_retry_delay(monkeypatch)
    opener, calls = flaky(run_log.open_regular_nofollow, failures)
    monkeypatch.setattr(run_log, "open_regular_nofollow", opener)
    if failures > 1:
        with pytest.raises(PermissionError):
            identity.pointer(work, "attempt", owner)
        assert calls[0] > 1
        assert identity.fingerprint == stale
        return
    pointer = identity.pointer(work, "attempt", owner)
    assert pointer is not None
    assert pointer["live"] is True
    assert calls == [2]
