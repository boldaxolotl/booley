"""Shared symlink refusal for advisory runtime storage."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.runtime.safe_storage import refuse_symlinks


def _linked_project(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(project/.booley, file)`` where the entry links to ``store``."""
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    (store / "runtime").mkdir(parents=True)
    (store / "runtime" / "x.json").write_text("{}")
    symlink_or_skip(project / ".booley", store, target_is_directory=True)
    return project / ".booley", project / ".booley" / "runtime" / "x.json"


def test_refuse_symlinks_follows_only_the_booley_directory_entry(tmp_path: Path) -> None:
    entry, record = _linked_project(tmp_path)

    # Without the Booley Directory the entry is an ordinary link and is refused.
    with pytest.raises(OSError, match="symlink storage is unavailable"):
        refuse_symlinks(record)
    # With it, the single entry is followed.
    refuse_symlinks(record, booley_dir=entry)

    # A link below the entry is still refused.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    symlink_or_skip(tmp_path / "store" / "runtime" / "link", elsewhere, target_is_directory=True)
    with pytest.raises(OSError, match="symlink storage is unavailable"):
        refuse_symlinks(entry / "runtime" / "link" / "y.json", booley_dir=entry)

    # A link above the entry is still refused, even when the spelling matches.
    alias = tmp_path / "alias"
    symlink_or_skip(alias, tmp_path / "proj", target_is_directory=True)
    with pytest.raises(OSError, match="symlink storage is unavailable"):
        refuse_symlinks(alias / ".booley" / "runtime" / "x.json", booley_dir=alias / ".booley")
