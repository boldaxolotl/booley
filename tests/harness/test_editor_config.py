"""Tests for harness._editor_config — the Console always launches VS Code."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from booley.config import editor as editor_config
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

    def test_gui_resolver_requires_the_application_executable(self, tmp_path, monkeypatch):
        application = tmp_path / "Visual Studio Code.app"
        executable = application / "Contents" / "MacOS" / "Electron"
        application.mkdir()
        monkeypatch.setattr(
            editor_config,
            "_editor_install_candidates",
            lambda: ((application, executable),),
        )
        assert editor_config.resolve_editor_install() is None

        executable.parent.mkdir(parents=True)
        executable.touch()
        assert editor_config.resolve_editor_install() == application

    def test_macos_candidates_cover_system_and_user_applications(self, monkeypatch):
        monkeypatch.setattr(editor_config.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: Path("/Users/test")))

        candidates = editor_config._editor_install_candidates()

        assert len(candidates) == 10
        assert candidates[0] == (
            Path("/Applications/Visual Studio Code.app"),
            Path("/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
        )
        assert candidates[-1] == (
            Path("/Users/test/Applications/Windsurf.app"),
            Path("/Users/test/Applications/Windsurf.app/Contents/MacOS/Windsurf"),
        )

    def test_windows_candidates_use_local_app_data(self, monkeypatch):
        monkeypatch.setattr(editor_config.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", "C:/Users/test/AppData/Local")

        candidates = editor_config._editor_install_candidates()

        assert len(candidates) == 5
        assert candidates[0][0] == Path(
            "C:/Users/test/AppData/Local/Programs/Microsoft VS Code/Code.exe"
        )
        assert all(application == executable for application, executable in candidates)

    def test_linux_has_no_native_gui_install_candidates(self, monkeypatch):
        monkeypatch.setattr(editor_config.sys, "platform", "linux")
        assert editor_config._editor_install_candidates() == ()


class TestResolvedEditor:
    def test_resolved_is_frozen(self):
        # Frozen so it can be safely shared as a module constant.
        r = ResolvedEditor(open=("code", "{file}"), open_at_line=("code",), diff=None)
        with pytest.raises(FrozenInstanceError):
            r.open = ("nope",)  # type: ignore[misc]
