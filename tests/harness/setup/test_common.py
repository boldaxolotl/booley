"""Tests for init's shared output helpers and the writer knobs init steps rely on.

``tests/runtime/test_guarded_write.py`` covers each ownership outcome of the shared
writer; these cases cover the newline and executable-bit knobs init's hooks use.
"""

from __future__ import annotations

import os
import re
import sys
from io import BytesIO, TextIOWrapper
from pathlib import Path

import pytest

from booley.harness import terminal_status as ts
from booley.harness.setup import common
from booley.harness.setup.common import (
    InitContext,
    WriteOutcome,
    configure_progress_output,
    guarded_write,
)
from booley.presentation import colors

MARKER = "# managed by test"


def test_progress_output_flushes_each_redirected_line(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = BytesIO()
    redirected = TextIOWrapper(raw, encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", redirected)

    configure_progress_output()
    print("Sandbox Image build started")

    assert raw.getvalue().decode("utf-8").splitlines() == ["Sandbox Image build started"]


@pytest.mark.parametrize(
    ("helper", "status"),
    [
        ("ok", ts.LineStatus.PASS),
        ("skip", ts.LineStatus.SKIP),
        ("note", ts.LineStatus.NOTE),
        ("warn", ts.LineStatus.WARN),
        ("err", ts.LineStatus.FAIL),
    ],
    ids=["ok", "skip", "note", "warn", "err"],
)
def test_setup_helpers_print_doctor_status_rows(
    helper: str,
    status: ts.LineStatus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)

    getattr(common, helper)("a message")

    assert capsys.readouterr().out == ts.status_line(status, "a message") + "\n"


def test_banner_is_the_doctor_section_header(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)

    common.banner("A title")

    assert capsys.readouterr().out == "\n" + ts.section_header("A title") + "\n"


_POSIX_MODE = pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")


@pytest.mark.parametrize(
    ("existing", "options", "outcome", "after"),
    [
        # A CRLF shebang is an ENOENT inside the container (QA_REPORT D0a).
        pytest.param(None, {"newline": "\n"}, WriteOutcome.WRITTEN, b"x\nbody\n", id="lf-forced"),
        pytest.param(
            b"x\r\nbody\r\n",
            {"newline": "\n", "dry_run": True},
            WriteOutcome.WRITTEN,
            b"x\r\nbody\r\n",
            id="dry-run-reports-crlf",
        ),
        pytest.param(
            b"x\r\nbody\r\n",
            {"newline": "\n"},
            WriteOutcome.WRITTEN,
            b"x\nbody\n",
            id="crlf-healed",
        ),
        pytest.param(
            b"custom hook\n",
            {"backup_suffix": ".pre-booley", "dry_run": True},
            WriteOutcome.BACKED_UP,
            b"custom hook\n",
            id="dry-run-never-backs-up",
        ),
        pytest.param(
            b"x\nbody\n",
            {"executable": True, "dry_run": True},
            WriteOutcome.WRITTEN,
            b"x\nbody\n",
            id="dry-run-reports-missing-exec-bit",
            marks=_POSIX_MODE,
        ),
    ],
)
def test_managed_write_plans_and_heals_line_endings_and_modes(
    tmp_path: Path, existing: bytes | None, options: dict, outcome: WriteOutcome, after: bytes
) -> None:
    target = tmp_path / "hook"
    if existing is not None:
        target.write_bytes(existing.replace(b"x", MARKER.encode()))
        target.chmod(0o644)

    result = guarded_write(target, f"{MARKER}\nbody\n", owner_marker=MARKER, **options)

    assert result is outcome
    assert target.read_bytes() == after.replace(b"x", MARKER.encode())
    assert not (tmp_path / "hook.pre-booley").exists()
    if options.get("executable"):
        assert target.stat().st_mode & 0o111 == 0


def test_dry_run_ignores_unrepresentable_windows_executable_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import guarded_write as guarded_write_module

    content = f"{MARKER}\nbody\n"
    target = tmp_path / "hook"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(guarded_write_module.os, "name", "nt")

    outcome = guarded_write(target, content, owner_marker=MARKER, executable=True, dry_run=True)

    assert outcome is WriteOutcome.UNCHANGED


class TestStepBanner:
    """Init's visible step numbers used to be hardcoded literals, so retiring a
    step left a permanent hole (1, 2, 3, 5, 8, 9, 9b, 10, 10b ... 12) that reads
    like skipped or failed work. The number is allocated at print time now."""

    def test_numbers_run_contiguously_from_one(self, capsys):
        ctx = InitContext()

        for title in ("first", "second", "third"):
            ctx.step_banner(title)

        out = capsys.readouterr().out
        assert "Step 1 — first" in out
        assert "Step 2 — second" in out
        assert "Step 3 — third" in out

    def test_no_init_module_hardcodes_a_step_number(self):
        """The drift guard: a new step must call ctx.step_banner, not bake in a
        literal that the next retirement turns into a hole."""
        harness = Path(__file__).resolve().parents[3] / "src" / "booley" / "harness"
        offenders = [
            p.name
            for p in harness.glob("init_*.py")
            if re.search(r'banner\(\s*[\'"]Step ', p.read_text(encoding="utf-8"))
        ]
        assert offenders == []
