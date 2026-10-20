"""Tests for the durable optional QA skill source selection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.runtime import qa_skill_selection as selection


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "Booley"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n", encoding="utf-8"
    )
    for name in selection.QA_SKILL_NAMES:
        skill = root / "qa" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# QA\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "feature", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return root


def test_absent_selection_is_disabled_and_ignores_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))

    assert selection.inspect_selection() is None
    assert selection.selection_path() == tmp_path / "home" / ".agents" / "booley-qa-skills.json"


def test_enable_records_dirty_feature_checkout_and_disable_recovers_without_parsing(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path)
    state = tmp_path / "state.json"
    (root / "untracked").write_text("dirty\n", encoding="utf-8")

    enabled = selection.enable(root / "qa", state)

    assert json.loads(state.read_text(encoding="utf-8")) == {"checkout_root": str(root.resolve())}
    assert selection.inspect_selection(state) == enabled
    state.write_text("not json", encoding="utf-8")
    with pytest.raises(selection.QaSkillSelectionError, match="cannot read"):
        selection.inspect_selection(state)
    selection.disable(state)
    assert selection.inspect_selection(state) is None


def test_current_release_selection_is_read_and_extra_fields_ignored(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    sample = Path(__file__).parent / "fixtures" / "qa-selection-v1.json"
    document = json.loads(sample.read_text(encoding="utf-8"))
    state.write_text(
        json.dumps({**document, "checkout_root": str(tmp_path), "surprise": True}),
        encoding="utf-8",
    )

    assert selection.inspect_selection(state) == selection.QaSkillSelection(str(tmp_path))


def test_enable_rejects_linked_worktree(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-b", "other", str(linked)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(selection.QaSkillSelectionError, match="primary"):
        selection.enable(linked, tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize(
    "missing",
    ["pyproject.toml", *(f"qa/{name}/SKILL.md" for name in sorted(selection.QA_SKILL_NAMES))],
)
def test_enable_rejects_incomplete_checkout(tmp_path: Path, missing: str) -> None:
    root = _checkout(tmp_path)
    (root / missing).unlink()

    with pytest.raises(selection.QaSkillSelectionError):
        selection.enable(root, tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()
