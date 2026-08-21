"""Tests for harness._editor_config — the Console always launches VS Code."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from booley.config.editor import VSCODE_EDITOR, ResolvedEditor, resolve_editor


class TestVSCodeEditor:
    def test_open_template(self):
        assert VSCODE_EDITOR.open == ("code", "--goto", "{file}")

    def test_open_at_line_template(self):
        assert VSCODE_EDITOR.open_at_line == ("code", "--goto", "{file}:{line}")

    def test_diff_template(self):
        assert VSCODE_EDITOR.diff == ("code", "--diff", "{left}", "{right}")

    def test_resolver_uses_first_supported_installed_editor(self):
        found = {"codium": "/opt/bin/codium"}

        editor = resolve_editor(found.get)

        assert editor is not None
        assert editor.open[0] == "/opt/bin/codium"
        assert editor.diff == ("/opt/bin/codium", "--diff", "{left}", "{right}")

    def test_resolver_returns_none_when_no_editor_is_installed(self):
        assert resolve_editor(lambda _command: None) is None


class TestResolvedEditor:
    def test_resolved_is_frozen(self):
        # Frozen so it can be safely shared as a module constant.
        r = ResolvedEditor(open=("code", "{file}"), open_at_line=("code",), diff=None)
        with pytest.raises(FrozenInstanceError):
            r.open = ("nope",)  # type: ignore[misc]
