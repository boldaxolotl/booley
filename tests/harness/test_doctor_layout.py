"""Doctor's console layout: section headers, one glyph per row, closing tables (#2430)."""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import date

import pytest

from booley.audit.diagnostic_results import Severity
from booley.audit.doctor_waivers import DoctorWaiver, DoctorWaivers
from booley.harness import booley as booley_cli
from booley.harness import doctor
from booley.presentation import colors
from tests.git_support import git
from tests.harness.doctor_run_support import run_doctor
from tests.harness.test_doctor import _patch_environment, _write_project


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch):
    """Plain text unless a test asks for colors."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)


def _reporter() -> doctor._Reporter:
    # private: a whole run cannot choose its findings, and these tests need one row of each kind.
    return doctor._Reporter.create()


def _plain_reporter_run(capsys, monkeypatch, *, colored: bool) -> str:
    """Drive one reporter through every row kind and return its console output."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", colored)
    reporter = _reporter()
    reporter.pass_("docker found")
    reporter.note_("design is large", "split it up")
    reporter.skip_("optional probe unavailable")
    warning = doctor.warning("sim.trace-unavailable", "trace unavailable", subject="sim_fast")
    reporter.warn_(warning, "declare trace_files")
    reporter.fail_("sandbox broken", "rebuild the image", check_id="sandbox.broken")
    reporter.finish()
    return capsys.readouterr().out


def test_phase_sections_render_as_headers_with_glyph_rows(capsys, monkeypatch, tmp_path):
    booley_dir = _write_project(tmp_path)
    (booley_dir / "booley.toml").write_bytes(b"[project\n")
    _patch_environment(monkeypatch, tmp_path, booley_dir)

    run_doctor(tmp_path)

    lines = capsys.readouterr().out.splitlines()
    start = lines.index("Booley Flow setup checks") - 1
    assert lines[start : start + 6] == [
        "",
        "Booley Flow setup checks",
        "  ·  Flow dry-runs skipped - project config invalid",
        "",
        "FuseSoC .core checks",
        "  ·  .core audit skipped - project config invalid",
    ]


def test_rows_drop_status_words_and_colors_strip_to_the_plain_layout(capsys, monkeypatch):
    plain = _plain_reporter_run(capsys, monkeypatch, colored=False)
    colored = _plain_reporter_run(capsys, monkeypatch, colored=True)
    output = plain.split("\nSummary")[0]
    lines = output.splitlines()

    assert "  ✓  docker found" in lines
    assert "  ·  design is large" in lines
    assert "  ·  optional probe unavailable" in lines
    warn_row = next(line for line in lines if line.startswith("  !  trace unavailable"))
    assert warn_row.endswith("  sim.trace-unavailable:sim_fast")
    assert "[" not in warn_row
    fail_row = next(line for line in lines if line.startswith("  ✗  sandbox broken"))
    assert fail_row.endswith("  sandbox.broken")
    assert "     → declare trace_files" in lines
    assert "     → rebuild the image" in lines
    assert "     → split it up" in lines
    assert not re.search(r"\b(PASS|WARN|FAIL|NOTE|SKIP)\b", output)
    assert "\033" not in plain
    assert "\033[2msim.trace-unavailable:sim_fast" in colored
    assert colors.strip_ansi(colored) == plain
    # A clean run, with colors on, ends in a bold green verdict and Summary line.
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    reporter = _reporter()
    reporter.pass_("fine")
    capsys.readouterr()
    reporter.finish()
    clean = capsys.readouterr().out
    assert colors.bold_green("no failures or warnings") in clean
    assert clean.rstrip("\n").endswith(colors.bold_green("  ✓ All clear. Booley is ready to use."))


@pytest.mark.parametrize(
    ("fail", "warn", "verdict"),
    [
        (0, 0, "  ✓ All clear. Booley is ready to use."),
        (
            0,
            2,
            "  ! 2 warnings. Resolve or explicitly waive every warning before calling setup clean.",
        ),
        (1, 1, "  ✗ 1 failure. Fix every failure before using Booley."),
        (2, 0, "  ✗ 2 failures. Fix every failure before using Booley."),
    ],
)
def test_finish_prints_verdict_problem_table_counts_and_exit_code(fail, warn, verdict, capsys):
    reporter = _reporter()
    reporter.pass_("fine")
    reporter.note_("just so you know")
    reporter.skip_("not applicable")
    for index in range(warn):
        reporter.warn_(doctor.warning("test.warn", f"warning {index}", subject=f"s{index}"))
    for index in range(fail):
        reporter.fail_(f"failure {index}")
    capsys.readouterr()

    exit_code = reporter.finish()
    lines = capsys.readouterr().out.splitlines()

    # The outcome is the last line, after a blank one, so it stands apart from the check
    # rows and the counts table. A warning does not raise the exit code past the failures.
    assert lines[-2:] == ["", verdict]
    assert exit_code == (1 if fail else 0)
    counts = [
        "  Status       Count",
        "  ──────────   ─────",
        "  ✓ passed     1",
        f"  ! warnings   {warn}",
        "  ~ waived     0",
        "  · notes      1",
        "  · skipped    1",
        f"  ✗ failed     {fail}",
    ]
    assert lines[:2] == ["", "Summary"]
    start = lines.index(counts[0])
    assert lines[start : start + len(counts)] == counts
    if not (fail or warn):
        assert lines[2] == "  ✓  no failures or warnings"
        return
    problem_rows = lines[4 : 4 + fail + warn]
    for index, row in enumerate(problem_rows[:fail]):
        assert row.startswith(f"  ✗ fail   failure {index}")
    for index, row in enumerate(problem_rows[fail:]):
        assert row.startswith(f"  ! warn   warning {index}")
        assert row.rstrip().endswith(f"test.warn:s{index}")
    problem_text = "\n".join(lines[2 : 4 + fail + warn])
    assert "fine" not in problem_text
    assert "just so you know" not in problem_text


@pytest.mark.parametrize("columns", [115, 18])
def test_summary_tables_fit_the_terminal_width(columns, capsys, monkeypatch):
    # #5312: a 115-column terminal wrapped the problem table mid-ID; 18 also shrinks the counts.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda *_: os.terminal_size((columns, 24)))
    reporter = _reporter()
    message = "the destination branch named by project data is missing from the Project " * 2
    reporter.warn_(doctor.warning("project-data.destination-branch-missing", message, subject="s"))
    capsys.readouterr()
    reporter.finish()

    lines = capsys.readouterr().out.splitlines()
    tables = lines[lines.index("Summary") + 1 : -2]
    assert max(map(colors.len_visible, tables)) <= columns


@pytest.mark.parametrize("leftover", ["env", "directory", "beside-current"])
def test_run_doctor_layout_error_uses_fail_glyph(capsys, monkeypatch, tmp_path, leftover):
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    if leftover == "env":
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", "")
        subject = "BOOLEY_PROJECT_DIR"
    else:
        (tmp_path / ".booley_project").mkdir()
        subject = str(tmp_path.resolve() / ".booley_project")
        if leftover == "beside-current":
            (tmp_path / ".booley").mkdir()
    monkeypatch.setattr(sys, "argv", ["booley", "doctor"])

    assert booley_cli.main() == 2

    output = capsys.readouterr()
    assert output.out == (
        f"  ✗  {subject} is retired: this release does not migrate Booley 0.3.x Projects; "
        "upgrade through Booley 0.4.x and run its `booley init` first, "
        "then upgrade to this release\n"
    )
    assert "ERROR:" not in output.err


def test_waived_findings_are_counted_but_not_listed_as_problems(capsys):
    reporter = _reporter()
    reporter.waived_(doctor.DiagnosticFinding(Severity.WAIVED, "old", "approved", "test.waived"))
    capsys.readouterr()

    reporter.finish()

    lines = capsys.readouterr().out.splitlines()
    assert "  ✓  no failures or warnings" in lines
    assert "  ~ waived     1" in lines


def test_expired_waiver_note_sits_under_its_own_section(capsys, tmp_path):
    reporter = _reporter()
    reporter.waivers = DoctorWaivers(
        path=tmp_path / "doctor-waivers.toml",
        expired=(DoctorWaiver("sim.old", None, "stale", date(2026, 1, 1), False),),
    )
    reporter.pass_("docker found")
    capsys.readouterr()

    reporter.finish()

    lines = capsys.readouterr().out.splitlines()
    header = lines.index("Waivers")
    assert lines[header + 1].startswith("  ·  expired Doctor waiver ignored: sim.old")
    assert header < lines.index("Summary")


def test_fail_row_and_problem_table_show_the_same_override(capsys):
    reporter = _reporter()
    reporter.display_overrides["login expired 2026-01-01"] = "login expired 3 days ago"

    reporter.fail_("login expired 2026-01-01")
    reporter.finish()

    out = capsys.readouterr().out
    assert out.count("login expired 3 days ago") == 2
    assert "2026-01-01" not in out
