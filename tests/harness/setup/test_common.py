"""Tests for setup.common.guarded_write — the single init clobber-guard.

Every scaffolding step routes its writes through guarded_write, so these
tests pin the ownership contract each historical per-site scheme (bare
.exists(), _SYSTEMD_MARKER, "already ours" hook sniffs) collapsed into.
"""

from __future__ import annotations

import os
import re
import sys
from io import BytesIO, TextIOWrapper
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from booley.harness.setup.common import (
    InitContext,
    WriteOutcome,
    configure_progress_output,
    guarded_write,
)

MARKER = "# managed by test"


def test_progress_output_flushes_each_redirected_line(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = BytesIO()
    redirected = TextIOWrapper(raw, encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", redirected)

    configure_progress_output()
    print("Session Image build started")

    assert raw.getvalue().decode("utf-8").splitlines() == ["Session Image build started"]


# ---------------------------------------------------------------------------
# create-only (owner_marker=None)
# ---------------------------------------------------------------------------


class TestCreateOnly:
    def test_creates_missing_file(self, tmp_path: Path):
        target = tmp_path / "sub" / "booley.toml"
        outcome = guarded_write(target, "skeleton\n")
        assert outcome is WriteOutcome.WRITTEN
        assert target.read_text(encoding="utf-8") == "skeleton\n"

    def test_never_touches_existing_file(self, tmp_path: Path):
        target = tmp_path / "booley.toml"
        target.write_text("user content\n", encoding="utf-8")
        outcome = guarded_write(target, "skeleton\n")
        assert outcome is WriteOutcome.SKIPPED
        assert target.read_text(encoding="utf-8") == "user content\n"

    def test_dry_run_reports_written_without_writing(self, tmp_path: Path):
        target = tmp_path / "booley.toml"
        outcome = guarded_write(target, "skeleton\n", dry_run=True)
        assert outcome is WriteOutcome.WRITTEN
        assert not target.exists()


# ---------------------------------------------------------------------------
# managed (owner_marker set)
# ---------------------------------------------------------------------------


class TestManaged:
    def test_creates_missing_file(self, tmp_path: Path):
        target = tmp_path / "unit.service"
        content = f"{MARKER}\nbody v1\n"
        assert guarded_write(target, content, owner_marker=MARKER) is WriteOutcome.WRITTEN
        assert target.read_text(encoding="utf-8") == content

    def test_refreshes_stale_booley_owned_file(self, tmp_path: Path):
        target = tmp_path / "unit.service"
        target.write_text(f"{MARKER}\nbody v1\n", encoding="utf-8")
        outcome = guarded_write(target, f"{MARKER}\nbody v2\n", owner_marker=MARKER)
        assert outcome is WriteOutcome.WRITTEN
        assert "body v2" in target.read_text(encoding="utf-8")

    def test_identical_content_is_unchanged(self, tmp_path: Path):
        target = tmp_path / "unit.service"
        content = f"{MARKER}\nbody\n"
        target.write_text(content, encoding="utf-8")
        assert guarded_write(target, content, owner_marker=MARKER) is WriteOutcome.UNCHANGED

    def test_refuses_foreign_file(self, tmp_path: Path):
        target = tmp_path / "unit.service"
        target.write_text("hand-rolled unit\n", encoding="utf-8")
        outcome = guarded_write(target, f"{MARKER}\nbody\n", owner_marker=MARKER)
        assert outcome is WriteOutcome.REFUSED
        assert target.read_text(encoding="utf-8") == "hand-rolled unit\n"

    def test_backs_up_foreign_file_when_suffix_given(self, tmp_path: Path):
        target = tmp_path / "commit-msg"
        target.write_text("#!/bin/sh\ncustom hook\n", encoding="utf-8")
        outcome = guarded_write(
            target,
            f"{MARKER}\nours\n",
            owner_marker=MARKER,
            backup_suffix=".pre-booley",
        )
        assert outcome is WriteOutcome.BACKED_UP
        backup = tmp_path / "commit-msg.pre-booley"
        assert backup.read_text(encoding="utf-8") == "#!/bin/sh\ncustom hook\n"
        assert "ours" in target.read_text(encoding="utf-8")

    def test_dry_run_never_writes_or_backs_up(self, tmp_path: Path):
        target = tmp_path / "commit-msg"
        target.write_text("custom hook\n", encoding="utf-8")
        outcome = guarded_write(
            target,
            f"{MARKER}\nours\n",
            owner_marker=MARKER,
            backup_suffix=".pre-booley",
            dry_run=True,
        )
        assert outcome is WriteOutcome.BACKED_UP
        assert target.read_text(encoding="utf-8") == "custom hook\n"
        assert not (tmp_path / "commit-msg.pre-booley").exists()

    def test_content_missing_its_own_marker_is_a_bug(self, tmp_path: Path):
        # Content without the marker would make the NEXT run refuse booley's
        # own file — reject the write-site bug loudly.
        with pytest.raises(ValueError, match="owner marker"):
            guarded_write(tmp_path / "x", "no marker here\n", owner_marker=MARKER)


# ---------------------------------------------------------------------------
# newline / executable knobs
# ---------------------------------------------------------------------------


class TestWriteKnobs:
    def test_lf_newline_forced(self, tmp_path: Path):
        # newline="\n" must defeat OS line translation (a CRLF shebang is an
        # ENOENT inside the container — QA_REPORT D0a).
        target = tmp_path / "hook"
        guarded_write(target, "#!/bin/sh\necho hi\n", newline="\n")
        assert b"\r" not in target.read_bytes()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
    def test_executable_bit_set_on_write(self, tmp_path: Path):
        target = tmp_path / "hook"
        guarded_write(target, f"{MARKER}\nbody\n", owner_marker=MARKER, executable=True)
        assert target.stat().st_mode & 0o111

    @pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
    def test_executable_bit_healed_on_unchanged(self, tmp_path: Path):
        content = f"{MARKER}\nbody\n"
        target = tmp_path / "hook"
        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)
        outcome = guarded_write(target, content, owner_marker=MARKER, executable=True)
        assert outcome is WriteOutcome.UNCHANGED
        assert target.stat().st_mode & 0o111


# ---------------------------------------------------------------------------
# Step numbering (F-2)
# ---------------------------------------------------------------------------


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

    def test_a_conditional_step_renumbers_rather_than_leaves_a_hole(self, capsys):
        """--scaffold (and --seed's single step) shift the sequence; they must
        never punch a gap into it."""
        ctx = InitContext()
        ctx.step_banner("only step that ran")

        assert "Step 1 — only step that ran" in capsys.readouterr().out

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
