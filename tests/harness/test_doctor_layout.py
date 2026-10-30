"""Doctor's console layout: section headers, one glyph per row, closing tables (#2430)."""

from __future__ import annotations

import argparse
import re

import pytest

from booley.audit.diagnostic_results import Severity
from booley.harness import colors, doctor


def _plain_reporter_run(capsys, monkeypatch, *, colored: bool) -> str:
    """Drive one reporter through every row kind and return its console output."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", colored)
    reporter = doctor._Reporter.create()
    reporter.pass_("docker found")
    reporter.note_("design is large", "split it up")
    reporter.skip_("optional probe unavailable")
    reporter.warn_(
        doctor.warning("sim.trace-unavailable", "trace unavailable", subject="sim_fast"),
        "declare trace_files",
    )
    reporter.fail_("sandbox broken", "rebuild the image", check_id="sandbox.broken")
    reporter.finish()
    return capsys.readouterr().out


def test_phase_sections_render_as_headers_with_glyph_rows(capsys, monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    reporter = doctor._Reporter.create()

    doctor._run_flow_and_core_phase(None, None, False, reporter, lambda *_args: None)

    assert capsys.readouterr().out.splitlines() == [
        "",
        "Booley Flow setup checks",
        "  ·  Flow dry-runs skipped - project config invalid",
        "",
        "FuseSoC .core checks",
        "  ·  .core audit skipped - project config invalid",
    ]


def test_reporter_rows_drop_status_words_and_brackets(capsys, monkeypatch):
    output = _plain_reporter_run(capsys, monkeypatch, colored=False).split("\nSummary")[0]
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


def test_colored_output_strips_to_the_plain_layout(capsys, monkeypatch):
    plain = _plain_reporter_run(capsys, monkeypatch, colored=False)
    colored = _plain_reporter_run(capsys, monkeypatch, colored=True)

    assert "\033" not in plain
    assert "\033[2msim.trace-unavailable:sim_fast" in colored
    assert colors.strip_ansi(colored) == plain
    assert "  ✓  docker found" in plain


def test_long_check_announcement_uses_running_glyph(capsys, monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)

    doctor._announce_long_check("synth deep check", "real EDA run; timeout 10m")

    assert capsys.readouterr().out == "  ▸  synth deep check (real EDA run; timeout 10m)\n"


def _finish_with(capsys, monkeypatch, *, fail: int = 0, warn: int = 0, extra: bool = True):
    """Finish a reporter holding the given active findings; return (exit code, lines)."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    reporter = doctor._Reporter.create()
    if extra:
        reporter.pass_("fine")
        reporter.note_("just so you know")
        reporter.skip_("not applicable")
    for index in range(warn):
        reporter.warn_(doctor.warning("test.warn", f"warning {index}", subject=f"s{index}"))
    for index in range(fail):
        reporter.fail_(f"failure {index}")
    capsys.readouterr()
    exit_code = reporter.finish()
    return exit_code, capsys.readouterr().out.splitlines()


@pytest.mark.parametrize("scenario", ["problems", "clean"])
def test_finish_prints_problem_table_and_counts(scenario, capsys, monkeypatch):
    fails, warns = (1, 1) if scenario == "problems" else (0, 0)
    _, lines = _finish_with(capsys, monkeypatch, fail=fails, warn=warns)

    counts = [
        "  Status       Count",
        "  ──────────   ─────",
        "  ✓ passed     1",
        f"  ! warnings   {warns}",
        "  ~ waived     0",
        "  · notes      1",
        "  · skipped    1",
        f"  ✗ failed     {fails}",
    ]
    assert lines[:2] == ["", "Summary"]
    start = lines.index(counts[0])
    assert lines[start : start + len(counts)] == counts
    if scenario == "clean":
        assert lines[2] == "  ✓  no failures or warnings"
    else:
        problem_rows = lines[4:6]
        assert problem_rows[0].startswith("  ✗ fail   failure 0")
        assert problem_rows[1].startswith("  ! warn   warning 0")
        assert problem_rows[1].rstrip().endswith("test.warn:s0")
        assert "fine" not in "\n".join(lines[2:7])
        assert "just so you know" not in "\n".join(lines[2:7])


@pytest.mark.parametrize(
    ("fails", "warns", "expected"),
    [
        pytest.param(2, 1, 1, id="fail"),
        pytest.param(0, 2, 0, id="warn"),
        pytest.param(0, 0, 0, id="clean"),
    ],
)
def test_finish_exit_code_counts_only_failures(fails, warns, expected, capsys, monkeypatch):
    exit_code, lines = _finish_with(capsys, monkeypatch, fail=fails, warn=warns)

    assert exit_code == expected
    assert f"  ✗ failed     {fails}" in lines
    assert f"  ! warnings   {warns}" in lines


def test_run_doctor_layout_error_uses_fail_glyph(capsys, monkeypatch, tmp_path):
    from booley.core.booley_layout import BooleyLayoutError

    monkeypatch.setattr(colors, "COLORS_ENABLED", False)

    def refuse(_root):
        raise BooleyLayoutError("layout is stale")

    monkeypatch.setattr("booley.core.booley_layout.validate_environment", lambda: None)
    monkeypatch.setattr("booley.core.booley_layout.require_current_layout", refuse)

    assert doctor.run_doctor(argparse.Namespace(), tmp_path) == 2
    assert capsys.readouterr().out == "  ✗  layout is stale\n"


def test_waived_findings_are_counted_but_not_listed_as_problems(capsys, monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    reporter = doctor._Reporter.create()
    reporter.waived_(
        doctor.DiagnosticFinding(Severity.WAIVED, "old issue", "approved", "test.waived")
    )
    capsys.readouterr()

    reporter.finish()

    lines = capsys.readouterr().out.splitlines()
    assert "  ✓  no failures or warnings" in lines
    assert "  ~ waived     1" in lines
