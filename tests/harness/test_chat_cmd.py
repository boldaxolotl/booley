"""Tests for ``booley chat``."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.config.settings import BackendConfigError
from booley.harness import chat_cmd


class _ExecCalledError(Exception):
    """Stop a mocked process replacement while preserving its arguments."""


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_run_replaces_process_with_configured_cli(tmp_path, monkeypatch, provider):
    project_dir = tmp_path / "configured-project"
    loaded: list[tuple[Path, Path]] = []
    executed: list[tuple[str, list[str]]] = []
    config = type("Config", (), {"provider": provider})()

    monkeypatch.setattr(chat_cmd, "resolve_checkout_project_dir", lambda _root: project_dir)
    monkeypatch.setattr(
        chat_cmd,
        "load_models_config",
        lambda root, *, project_dir: loaded.append((root, project_dir)),
    )
    monkeypatch.setattr(chat_cmd, "get_backend_config", lambda: config)

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)

    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(), tmp_path)

    assert loaded == [(tmp_path, project_dir)]
    assert executed == [(provider, [provider])]


def test_run_reports_missing_configured_cli(tmp_path, monkeypatch, capsys):
    config = type("Config", (), {"provider": "codex"})()
    monkeypatch.setattr(chat_cmd, "resolve_checkout_project_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_models_config", lambda _root, *, project_dir: None)
    monkeypatch.setattr(chat_cmd, "get_backend_config", lambda: config)
    monkeypatch.setattr(
        chat_cmd.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError),
    )

    assert chat_cmd.run(argparse.Namespace(), tmp_path) == 2
    assert "configured agent CLI 'codex' was not found on PATH" in capsys.readouterr().err


def test_run_reports_invalid_project_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chat_cmd, "resolve_checkout_project_dir", lambda root: root)
    monkeypatch.setattr(
        chat_cmd,
        "load_models_config",
        lambda _root, *, project_dir: (_ for _ in ()).throw(
            BackendConfigError("invalid provider")
        ),
    )

    assert chat_cmd.run(argparse.Namespace(), tmp_path) == 2
    assert "could not resolve the Project's agent provider" in capsys.readouterr().err


def test_run_does_not_mask_unexpected_runtime_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_cmd, "resolve_checkout_project_dir", lambda root: root)
    monkeypatch.setattr(
        chat_cmd,
        "load_models_config",
        lambda _root, *, project_dir: (_ for _ in ()).throw(RuntimeError("backend defect")),
    )

    with pytest.raises(RuntimeError, match="backend defect"):
        chat_cmd.run(argparse.Namespace(), tmp_path)


def test_run_loads_provider_from_resolved_project_directory(tmp_path, monkeypatch):
    project_root = tmp_path / "checkout"
    project_root.mkdir()
    project_dir = tmp_path / "external-project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text(
        '[agent]\nprovider = "codex"\n',
        encoding="utf-8",
    )
    executed: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(chat_cmd, "resolve_checkout_project_dir", lambda _root: project_dir)

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)

    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(), project_root)

    assert executed == [("codex", ["codex"])]
