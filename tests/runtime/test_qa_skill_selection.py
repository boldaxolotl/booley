"""Tests for the durable optional QA skill source selection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.runtime import qa_skill_selection as selection


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "Booley"
    (root / "src" / "booley").mkdir(parents=True)
    (root / "src" / "booley" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n", encoding="utf-8"
    )
    for name in selection.QA_SKILL_NAMES:
        skill = root / "qa" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# QA\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
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
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, revision


def test_absent_selection_is_disabled_and_ignores_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))

    assert selection.inspect_selection() is None
    assert selection.selection_path() == tmp_path / "home" / ".agents" / "booley-qa-skills.json"


def test_enable_revalidates_and_disable_recovers_without_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, revision = _checkout(tmp_path)
    state = tmp_path / "state.json"
    monkeypatch.setattr(selection.tempfile, "gettempdir", lambda: "/not-this-test-root")

    enabled = selection.enable(root, revision[:12], state)

    assert enabled.source_revision == revision
    assert selection.validate_selection(revision[:12], state) == enabled
    state.write_text("not json", encoding="utf-8")
    selection.disable(state)
    assert selection.inspect_selection(state) is None


def test_strict_selection_rejects_unknown_fields(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checkout_root": str(tmp_path.resolve()),
                "source_revision": "a" * 40,
                "surprise": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(selection.QaSkillSelectionError, match="unknown"):
        selection.inspect_selection(state)


def test_validation_rejects_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, revision = _checkout(tmp_path)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-b", "other", str(linked)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(selection.tempfile, "gettempdir", lambda: "/not-this-test-root")

    with pytest.raises(selection.QaSkillSelectionError, match="linked worktree"):
        selection.validate_checkout(linked, revision[:12])
