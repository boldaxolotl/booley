"""Deterministic release changelog checker contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "release_changelog.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_changelog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text() -> str:
    return "# Changelog\n\n## 1.2.0 - 31 AUG 2026\n\n### Bug fixes\n\n- Fixed it.\n"


def test_sync_and_validate_require_byte_identical_mirror(tmp_path: Path) -> None:
    checker = _load()
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "packaged.md"
    root.write_text(_text(), encoding="utf-8")
    packaged.write_text("drift\n", encoding="utf-8")

    with pytest.raises(checker.ChangelogError, match="differs"):
        checker.validate(root, packaged)

    checker.synchronize(root, packaged)
    checker.validate(root, packaged, target="1.2.0")
    assert root.read_bytes() == packaged.read_bytes()


def test_target_and_supplied_notes_must_match_exactly(tmp_path: Path) -> None:
    checker = _load()
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "packaged.md"
    notes = tmp_path / "notes.md"
    root.write_text(_text(), encoding="utf-8")
    packaged.write_text(_text(), encoding="utf-8")
    notes.write_text("different\n", encoding="utf-8")

    with pytest.raises(checker.ChangelogError, match="differ"):
        checker.validate(root, packaged, target="1.2.0", notes_file=notes)

    notes.write_text(checker.release_entry(_text(), "1.2.0").body, encoding="utf-8")
    checker.validate(root, packaged, target="1.2.0", notes_file=notes)
