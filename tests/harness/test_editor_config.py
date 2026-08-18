"""Tests for harness._editor_config — the Console always launches VS Code."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from booley.config.editor import VSCODE_EDITOR, ResolvedEditor


class TestVSCodeEditor:
    def test_open_template(self):
        assert VSCODE_EDITOR.open == ("code", "--goto", "{file}")

    def test_open_at_line_template(self):
        assert VSCODE_EDITOR.open_at_line == ("code", "--goto", "{file}:{line}")

    def test_diff_template(self):
        assert VSCODE_EDITOR.diff == ("code", "--diff", "{left}", "{right}")


class TestResolvedEditor:
    def test_resolved_is_frozen(self):
        # Frozen so it can be safely shared as a module constant.
        r = ResolvedEditor(open=("code", "{file}"), open_at_line=("code",), diff=None)
        with pytest.raises(FrozenInstanceError):
            r.open = ("nope",)  # type: ignore[misc]
