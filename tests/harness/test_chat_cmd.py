"""Tests for ``booley chat``."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.config.settings import BackendConfigError
from booley.harness import chat_cmd


class _ExecCalledError(Exception):
    """Stop a mocked process replacement while preserving its arguments."""


@pytest.fixture(autouse=True)
def plain_codex_launch(monkeypatch):
    monkeypatch.setattr(chat_cmd, "resolve_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd.codex_chat, "command", lambda _directory: ("codex", ["codex"]))


def test_run_uses_named_codex_command(tmp_path, monkeypatch):
    config = type("Config", (), {"provider": "codex"})()
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: config)
    monkeypatch.setattr(
        chat_cmd.codex_chat, "command", lambda _directory: ("/bin/codex", ["S17", "--no-daemon"])
    )
    executed = []

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(), tmp_path)
    assert executed == [("/bin/codex", ["S17", "--no-daemon"])]


@pytest.mark.parametrize("error", [FileNotFoundError, ValueError, RuntimeError])
def test_runtime_directory_failure_keeps_plain_codex_launch(tmp_path, monkeypatch, capsys, error):
    config = type("Config", (), {"provider": "codex"})()
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: config)
    monkeypatch.setattr(
        chat_cmd, "resolve_booley_dir", lambda _root: (_ for _ in ()).throw(error("bad runtime"))
    )
    executed = []

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(), tmp_path)
    assert executed == [("codex", ["codex"])]
    assert "Codex tab handle is unavailable: bad runtime" in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_run_replaces_process_with_configured_cli(tmp_path, monkeypatch, provider):
    booley_dir = tmp_path / "configured-project"
    loaded: list[tuple[Path, Path]] = []
    executed: list[tuple[str, list[str]]] = []
    config = type("Config", (), {"provider": provider})()

    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda _root: booley_dir)
    monkeypatch.setattr(
        chat_cmd,
        "load_agent_settings",
        lambda root, *, booley_dir: loaded.append((root, booley_dir)),
    )
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: config)

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)

    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(), tmp_path)

    assert loaded == [(tmp_path, booley_dir)]
    assert executed == [(provider, [provider])]


def test_run_reports_missing_configured_cli(tmp_path, monkeypatch, capsys):
    config = type("Config", (), {"provider": "codex"})()
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: config)
    monkeypatch.setattr(
        chat_cmd.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError),
    )

    assert chat_cmd.run(argparse.Namespace(), tmp_path) == 2
    assert "configured agent CLI 'codex' was not found on PATH" in capsys.readouterr().err


def test_run_reports_invalid_project_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(
        chat_cmd,
        "load_agent_settings",
        lambda _root, *, booley_dir: (_ for _ in ()).throw(BackendConfigError("invalid provider")),
    )

    assert chat_cmd.run(argparse.Namespace(), tmp_path) == 2
    assert "could not resolve the Project's agent provider" in capsys.readouterr().err


def test_run_does_not_mask_unexpected_runtime_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(
        chat_cmd,
        "load_agent_settings",
        lambda _root, *, booley_dir: (_ for _ in ()).throw(RuntimeError("backend defect")),
    )

    with pytest.raises(RuntimeError, match="backend defect"):
        chat_cmd.run(argparse.Namespace(), tmp_path)


def test_run_loads_provider_from_resolved_booley_directory(tmp_path, monkeypatch):
    project_root = tmp_path / "checkout"
    project_root.mkdir()
    booley_dir = tmp_path / "external-project"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        '[agent]\nprovider = "codex"\n',
        encoding="utf-8",
    )
    executed: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda _root: booley_dir)

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)

    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(), project_root)

    assert executed == [("codex", ["codex"])]


def test_worktree_provider_config_uses_the_shared_runtime_handle_map(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import reset_cache, resolve_booley_dir

    worktree = tmp_path / "worktree"
    local = worktree / ".booley"
    shared = tmp_path / "project" / ".booley"
    local.mkdir(parents=True)
    shared.mkdir(parents=True)
    (local / "booley.toml").write_text('[agent]\nprovider="codex"\n')
    (shared / "booley.toml").write_text('[agent]\nprovider="claude"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(shared))
    monkeypatch.setattr(chat_cmd, "resolve_booley_dir", resolve_booley_dir)
    directories = []
    monkeypatch.setattr(
        chat_cmd.codex_chat,
        "command",
        lambda directory: (directories.append(directory) or "codex", ["S17", "--no-daemon"]),
    )
    monkeypatch.setattr(
        chat_cmd.os, "execvp", lambda *_args: (_ for _ in ()).throw(_ExecCalledError)
    )
    reset_cache()
    try:
        with pytest.raises(_ExecCalledError):
            chat_cmd.run(argparse.Namespace(), worktree)
        assert directories == [shared]
        assert chat_cmd.get_agent_settings().provider == "codex"
    finally:
        reset_cache()
