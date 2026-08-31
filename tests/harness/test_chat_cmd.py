"""Tests for ``booley chat``."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.harness import chat_cmd


class _ExecCalledError(Exception):
    """Stop a mocked process replacement while preserving its arguments."""


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_run_replaces_process_with_configured_cli(tmp_path, monkeypatch, provider):
    loaded: list[Path] = []
    executed: list[tuple[str, list[str]]] = []
    config = type("Config", (), {"provider": provider})()

    monkeypatch.setattr(chat_cmd, "load_models_config", loaded.append)
    monkeypatch.setattr(chat_cmd, "get_backend_config", lambda: config)

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)

    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(), tmp_path)

    assert loaded == [tmp_path]
    assert executed == [(provider, [provider])]


def test_run_reports_missing_configured_cli(tmp_path, monkeypatch, capsys):
    config = type("Config", (), {"provider": "codex"})()
    monkeypatch.setattr(chat_cmd, "load_models_config", lambda _root: None)
    monkeypatch.setattr(chat_cmd, "get_backend_config", lambda: config)
    monkeypatch.setattr(
        chat_cmd.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError),
    )

    assert chat_cmd.run(argparse.Namespace(), tmp_path) == 2
    assert "configured agent CLI 'codex' was not found on PATH" in capsys.readouterr().err


def test_run_reports_invalid_project_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        chat_cmd,
        "load_models_config",
        lambda _root: (_ for _ in ()).throw(ValueError("invalid provider")),
    )

    assert chat_cmd.run(argparse.Namespace(), tmp_path) == 2
    assert "could not resolve the Project's agent provider" in capsys.readouterr().err
