"""Tests for the shared clobber-guarded writer at its runtime import path.

The full ownership contract is pinned by ``tests/harness/setup/test_common.py``
through the init re-export; these tests cover each outcome at the new path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.harness.setup import common
from booley.runtime import atomic_files
from booley.runtime.guarded_write import WriteOutcome, guarded_write

MARKER = "# managed by test"


def test_init_common_reexports_the_shared_writer() -> None:
    # The init path is a compatibility re-export, not a second implementation.
    assert common.guarded_write is guarded_write
    assert common.WriteOutcome is WriteOutcome


def test_create_only_file_becomes_user_owned(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "booley.toml"

    assert guarded_write(target, "skeleton\n") is WriteOutcome.WRITTEN
    target.write_text("user content\n", encoding="utf-8")
    assert guarded_write(target, "skeleton\n") is WriteOutcome.SKIPPED
    assert target.read_text(encoding="utf-8") == "user content\n"


def test_managed_file_is_refreshed_then_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "unit.service"
    target.write_text(f"{MARKER}\nbody v1\n", encoding="utf-8")
    content = f"{MARKER}\nbody v2\n"

    assert guarded_write(target, content, owner_marker=MARKER) is WriteOutcome.WRITTEN
    assert guarded_write(target, content, owner_marker=MARKER) is WriteOutcome.UNCHANGED
    assert target.read_text(encoding="utf-8") == content


def test_foreign_file_is_refused_or_backed_up(tmp_path: Path) -> None:
    refused = tmp_path / "unit.service"
    refused.write_text("hand-rolled unit\n", encoding="utf-8")
    backed_up = tmp_path / "commit-msg"
    backed_up.write_text("custom hook\n", encoding="utf-8")
    content = f"{MARKER}\nours\n"

    assert guarded_write(refused, content, owner_marker=MARKER) is WriteOutcome.REFUSED
    assert refused.read_text(encoding="utf-8") == "hand-rolled unit\n"
    outcome = guarded_write(backed_up, content, owner_marker=MARKER, backup_suffix=".pre-booley")
    assert outcome is WriteOutcome.BACKED_UP
    assert (tmp_path / "commit-msg.pre-booley").read_text(encoding="utf-8") == "custom hook\n"
    assert backed_up.read_text(encoding="utf-8") == content


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "booley.toml"

    assert guarded_write(target, "skeleton\n", dry_run=True) is WriteOutcome.WRITTEN
    assert not target.exists()


def test_content_missing_its_own_marker_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="owner marker"):
        guarded_write(tmp_path / "x", "no marker here\n", owner_marker=MARKER)


def _no_temp_files(directory: Path) -> bool:
    return not [p for p in directory.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
@pytest.mark.parametrize("kind", ["owned", "foreign", "missing"])
def test_symlinked_target_is_refused_and_untouched(tmp_path: Path, kind: str, dry_run: bool):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    destination = tmp_path / "outside" / "destination"
    destination.parent.mkdir()
    content = f"{MARKER}\nours\n"
    if kind == "owned":
        destination.write_text(f"{MARKER}\nstale\n", encoding="utf-8")
    elif kind == "foreign":
        destination.write_text("someone else's file\n", encoding="utf-8")
    link = hooks / "commit-msg"
    symlink_or_skip(link, destination)
    planted_target = link.readlink()
    before = destination.read_bytes() if destination.exists() else None
    # A missing destination is the create-only case: no marker, no backup suffix.
    policy = {} if kind == "missing" else {"owner_marker": MARKER, "backup_suffix": ".pre-booley"}

    outcome = guarded_write(link, content, dry_run=dry_run, **policy)

    assert outcome is WriteOutcome.REFUSED
    assert link.is_symlink()
    assert link.readlink() == planted_target
    assert (destination.read_bytes() if destination.exists() else None) == before
    assert sorted(p.name for p in hooks.iterdir()) == ["commit-msg"]
    assert sorted(p.name for p in destination.parent.iterdir()) == (
        ["destination"] if before is not None else []
    )


def test_successive_foreign_backups_keep_every_original(tmp_path: Path):
    target = tmp_path / "commit-msg"
    content = f"{MARKER}\nours\n"
    target.write_text("first original\n", encoding="utf-8")
    assert (
        guarded_write(target, content, owner_marker=MARKER, backup_suffix=".pre-booley")
        is WriteOutcome.BACKED_UP
    )

    target.write_text("second original\n", encoding="utf-8")
    assert (
        guarded_write(target, content, owner_marker=MARKER, backup_suffix=".pre-booley")
        is WriteOutcome.BACKED_UP
    )

    assert (tmp_path / "commit-msg.pre-booley").read_text(encoding="utf-8") == "first original\n"
    assert (tmp_path / "commit-msg.pre-booley.1").read_text(
        encoding="utf-8"
    ) == "second original\n"
    assert target.read_text(encoding="utf-8") == content


def test_backup_skips_a_symlinked_backup_name(tmp_path: Path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    outside = tmp_path / "outside"
    target = hooks / "commit-msg"
    target.write_text("foreign\n", encoding="utf-8")
    symlink_or_skip(hooks / "commit-msg.pre-booley", outside)  # dangling
    planted_target = (hooks / "commit-msg.pre-booley").readlink()

    guarded_write(target, f"{MARKER}\nours\n", owner_marker=MARKER, backup_suffix=".pre-booley")

    assert not outside.exists()
    assert (hooks / "commit-msg.pre-booley").readlink() == planted_target
    assert (hooks / "commit-msg.pre-booley.1").read_text(encoding="utf-8") == "foreign\n"


@pytest.mark.parametrize("existing", [True, False], ids=["rewrite", "create"])
def test_failed_write_keeps_previous_bytes_without_temp(tmp_path: Path, existing: bool):
    target = tmp_path / "unit.service"
    if existing:
        target.write_text(f"{MARKER}\nold\n", encoding="utf-8")
    # A lone surrogate cannot be encoded as UTF-8: the write fails before it starts.
    content = f"{MARKER}\nbad \ud800\n"

    with pytest.raises(UnicodeEncodeError):
        guarded_write(target, content, owner_marker=MARKER)

    if existing:
        assert target.read_text(encoding="utf-8") == f"{MARKER}\nold\n"
    else:
        assert not target.exists()
    assert _no_temp_files(tmp_path)


def test_failed_replace_keeps_previous_bytes_without_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "unit.service"
    target.write_bytes(f"{MARKER}\nold\n".encode())

    def broken_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    # The temp file is fully written and synced by now: only the swap fails.
    monkeypatch.setattr(atomic_files, "replace_file", broken_replace)

    with pytest.raises(OSError, match="replace failed"):
        guarded_write(target, f"{MARKER}\nnew\n", owner_marker=MARKER)

    assert target.read_bytes() == f"{MARKER}\nold\n".encode()
    assert _no_temp_files(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
class TestExecutableMode:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [(0o700, 0o700), (0o600, 0o700), (0o644, 0o755), (0o755, 0o755)],
        ids=["0o700", "0o600", "0o644", "0o755"],
    )
    def test_rewrite_never_widens_mode(self, tmp_path: Path, mode: int, expected: int):
        target = tmp_path / "pre-push"
        target.write_text(f"{MARKER}\nold\n", encoding="utf-8")
        target.chmod(mode)

        guarded_write(target, f"{MARKER}\nnew\n", owner_marker=MARKER, executable=True)

        assert target.stat().st_mode & 0o7777 == expected

    @pytest.mark.parametrize(
        ("mode", "expected"), [(0o600, 0o700), (0o644, 0o755)], ids=["0o600", "0o644"]
    )
    def test_unchanged_file_is_healed_without_widening(
        self, tmp_path: Path, mode: int, expected: int
    ):
        target = tmp_path / "pre-push"
        content = f"{MARKER}\nsame\n"
        target.write_text(content, encoding="utf-8")
        target.chmod(mode)

        outcome = guarded_write(target, content, owner_marker=MARKER, executable=True)

        assert outcome is WriteOutcome.UNCHANGED
        assert target.stat().st_mode & 0o7777 == expected

    def test_plain_rewrite_keeps_owner_only_mode(self, tmp_path: Path):
        target = tmp_path / "unit.service"
        target.write_text(f"{MARKER}\nold\n", encoding="utf-8")
        target.chmod(0o600)

        guarded_write(target, f"{MARKER}\nnew\n", owner_marker=MARKER)

        assert target.stat().st_mode & 0o7777 == 0o600

    def test_dry_run_accepts_owner_only_executable_mode(self, tmp_path: Path):
        target = tmp_path / "pre-push"
        content = f"{MARKER}\nsame\n"
        target.write_text(content, encoding="utf-8")
        target.chmod(0o700)

        assert (
            guarded_write(target, content, owner_marker=MARKER, executable=True, dry_run=True)
            is WriteOutcome.UNCHANGED
        )

    def test_new_files_get_the_default_modes(self, tmp_path: Path):
        plain = tmp_path / "plain"
        adapter = tmp_path / "adapter"

        guarded_write(plain, "x\n")
        guarded_write(adapter, f"{MARKER}\n", owner_marker=MARKER, executable=True)

        assert plain.stat().st_mode & 0o7777 == 0o644
        assert adapter.stat().st_mode & 0o7777 == 0o755
