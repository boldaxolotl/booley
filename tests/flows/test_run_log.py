"""Tests for the Flow-neutral durable run-log interface."""

import pytest

from booley.flows import run_log
from tests.conftest import symlink_or_skip
from tests.fusesoc_test_support import symlinked_booley_dir

# ---------------------------------------------------------------------------
# write_run_log
# ---------------------------------------------------------------------------


class TestWriteRunLog:
    """run.log — the durable raw-output copy that survives stdout truncation."""

    def test_filename_is_exactly_run_log(self, tmp_path):
        # CONTRACT: the simulate summary prints <work_dir>/run.log — the
        # filename must not drift.
        path = run_log.write_run_log(tmp_path, "hello\nworld\n")
        assert path == tmp_path / "run.log"
        assert run_log.RUN_LOG_NAME == "run.log"
        assert path.read_text(encoding="utf-8") == "hello\nworld\n"

    def test_under_cap_written_verbatim(self, tmp_path):
        text = "line\n" * 100
        run_log.write_run_log(tmp_path, text, max_bytes=10_000)
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content == text
        assert "TRUNCATED" not in content

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

    def test_none_cap_preserves_unabridged_output(self, tmp_path):
        text = "complete simulator evidence\n"
        run_log.write_run_log(tmp_path, text, max_bytes=None)
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == text

    def test_overwrites_previous_log(self, tmp_path):
        run_log.write_run_log(tmp_path, "first run")
        run_log.write_run_log(tmp_path, "second run")
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == "second run"

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
# begin_run_log / run_log_is_current — the F-26 staleness guard
# ---------------------------------------------------------------------------


class TestRunLogHeader:
    """A run.log must never read as live progress while holding old bytes."""

    def test_begin_erases_the_previous_runs_output(self, tmp_path):
        run_log.write_run_log(tmp_path, "TEST PASSED\nold verdict\n")
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert "TEST PASSED" not in content
        assert content.startswith(f"{run_log.RUN_LOG_HEADER_PREFIX} ")
        assert "run=run-2 flow=sim target=sim_fifo started=" in content
        assert run_log.RUN_LOG_PENDING in content

    def test_header_fields_parse_back(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="lint", target="lint_top", run="lint-7")
        header = run_log.read_run_log_header(tmp_path)
        assert header == {
            "run": "lint-7",
            "flow": "lint",
            "target": "lint_top",
            "started": header["started"],
        }
        assert header["started"].endswith("Z")

    def test_write_preserves_the_header(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        run_log.write_run_log(tmp_path, "[SIM_RESULT] PASSED\n")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content.splitlines()[0].startswith(run_log.RUN_LOG_HEADER_PREFIX)
        assert run_log.RUN_LOG_PENDING not in content
        assert content.endswith("[SIM_RESULT] PASSED\n")
        assert run_log.read_run_log_header(tmp_path)["run"] == "run-2"

    def test_cap_accounts_for_the_preserved_header(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        cap = 500
        run_log.write_run_log(tmp_path, "line\n" * 500, max_bytes=cap)
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= cap
        assert raw.startswith(run_log.RUN_LOG_HEADER_PREFIX.encode())

    def test_headerless_log_is_written_verbatim(self, tmp_path):
        # Paths that never call begin_run_log keep the historical shape.
        run_log.write_run_log(tmp_path, "raw output\n")
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == "raw output\n"
        assert run_log.read_run_log_header(tmp_path) is None

    def test_is_current_only_after_the_output_lands(self, tmp_path):
        assert run_log.run_log_is_current(tmp_path, "run-2") is False  # no log at all
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        assert run_log.run_log_is_current(tmp_path, "run-2") is False  # still pending
        run_log.write_run_log(tmp_path, "[SIM_RESULT] FAILED\n")
        assert run_log.run_log_is_current(tmp_path, "run-2") is True

    def test_is_current_rejects_another_run(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-1")
        run_log.write_run_log(tmp_path, "[SIM_RESULT] PASSED\n")
        assert run_log.run_log_is_current(tmp_path, "run-2") is False

    def test_is_current_rejects_a_headerless_log(self, tmp_path):
        run_log.write_run_log(tmp_path, "TEST PASSED\n")
        assert run_log.run_log_is_current(tmp_path, "run-2") is False

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
            tmp_path,
            "cycle 100\ncycle 200\n",
            elapsed_s=42.0,
            line_count=2,
            idle_s=3.0,
        )
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content.splitlines()[0].startswith(run_log.RUN_LOG_HEADER_PREFIX)
        assert "42s elapsed, 2 output line(s), last output 3s ago" in content
        assert "cycle 200" in content

    def test_progress_never_reads_as_a_finished_run(self, tmp_path):
        # The whole point of the marker prefix: a live tail is not a verdict.
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        run_log.write_run_log_progress(tmp_path, "TEST SUCCEEDED\n", elapsed_s=5.0, line_count=1)
        assert run_log.run_log_is_current(tmp_path, "run-9") is False
        run_log.write_run_log(tmp_path, "TEST SUCCEEDED\n[SIM_RESULT] PASSED\n")
        assert run_log.run_log_is_current(tmp_path, "run-9") is True

    def test_progress_keeps_the_tail_when_capped(self, tmp_path):
        run_log.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        run_log.write_run_log_progress(
            tmp_path,
            "".join(f"line {i}\n" for i in range(2000)),
            elapsed_s=1.0,
            line_count=2000,
            max_bytes=400,
        )
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= 400 + len(run_log.RUN_LOG_HEADER_PREFIX) + 200
        assert b"line 1999" in raw


def test_begin_run_log_never_truncates_unrelated_file(tmp_path, monkeypatch):
    from contextlib import suppress

    from booley.flows.run_log import begin_run_log
    from tests.conftest import symlink_or_skip

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
            begin_run_log(evidence, flow="sim", target="sim", owner=case_root)
        if sentinel.read_text() != "retained evidence":
            failures.append(case)
    assert failures == [], f"retained evidence was truncated: {failures}"


def test_begin_run_log_refreshes_under_symlinked_parent(tmp_path):
    from tests.conftest import symlink_or_skip

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
    from tests.conftest import symlink_or_skip

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
