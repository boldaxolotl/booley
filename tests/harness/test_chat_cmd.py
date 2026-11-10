"""Tests for ``booley chat``."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pytest

from booley.config.agent import AgentSettings
from booley.config.settings import BackendConfigError
from booley.harness import booley as booley_cli
from booley.harness import chat_cmd
from booley.runtime.pid import ProcessIdentity
from booley.runtime.session_activity import process_session_key


class _ExecCalledError(Exception):
    """Stop a mocked process replacement while preserving its arguments."""


@pytest.fixture(autouse=True)
def inline_launch(monkeypatch):
    """Run the launch in-process: these tests mock the exec and must not fork pytest."""
    monkeypatch.setattr(chat_cmd, "run_foreground_child", lambda launch: launch())


@pytest.fixture(autouse=True)
def plain_codex_launch(monkeypatch):
    monkeypatch.setattr(chat_cmd, "resolve_common_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd.codex_chat, "command", lambda _directory: ("codex", ["codex"]))


@pytest.fixture(autouse=True)
def deterministic_claude_launch(monkeypatch):
    monkeypatch.setattr(
        chat_cmd,
        "capture_process_identity",
        lambda _pid: ProcessIdentity(98765, "fixture", 101),
        raising=False,
    )
    monkeypatch.setattr(chat_cmd, "reserve_session_handle", lambda *_args: "S17", raising=False)


@pytest.fixture(autouse=True)
def cli_on_path(monkeypatch):
    """Keep launch tests independent of which agent CLIs the host has installed."""
    monkeypatch.setattr(shutil, "which", lambda name, mode=None: f"/usr/bin/{name}")


def _configure_provider(monkeypatch, provider):
    """Point ``run`` at ``provider`` without reading a real Project config."""
    config = type("Config", (), {"default_cli": provider})()
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: config)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_missing_cli_prints_only_the_error(tmp_path, monkeypatch, capsys, provider):
    _configure_provider(monkeypatch, provider)
    monkeypatch.setattr(shutil, "which", lambda name, mode=None: None)
    monkeypatch.setattr(
        chat_cmd.os, "execvp", lambda *_args: (_ for _ in ()).throw(FileNotFoundError)
    )

    assert chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path) == 2

    captured = capsys.readouterr()
    assert captured.err == (
        f"ERROR: configured agent CLI '{provider}' was not found on PATH.\n"
    ), "missing CLI must not announce startup"
    assert captured.out == ""


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_notice_is_the_last_line_before_exec(tmp_path, monkeypatch, capsys, provider):
    _configure_provider(monkeypatch, provider)
    if provider == "codex":
        monkeypatch.setattr(
            chat_cmd.codex_chat,
            "command",
            lambda _directory: (_ for _ in ()).throw(RuntimeError("no handle")),
        )
    seen = []

    def execvp(executable, argv):
        seen.append(capsys.readouterr().err)
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)

    notice = f"Booley is starting the {provider} interactive agent.\n"
    note = "NOTE: Codex tab handle is unavailable: no handle\n" if provider == "codex" else ""
    assert seen == [note + notice]


def test_run_uses_named_codex_command(tmp_path, monkeypatch):
    config = type("Config", (), {"default_cli": "codex"})()
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
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)
    assert executed == [("/bin/codex", ["S17", "--no-daemon"])]


@pytest.mark.parametrize("error", [FileNotFoundError, ValueError, RuntimeError])
def test_runtime_directory_failure_keeps_plain_codex_launch(tmp_path, monkeypatch, capsys, error):
    config = type("Config", (), {"default_cli": "codex"})()
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: config)
    monkeypatch.setattr(
        chat_cmd,
        "resolve_common_booley_dir",
        lambda _root: (_ for _ in ()).throw(error("bad runtime")),
    )
    executed = []

    def execvp(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)
    assert executed == [("codex", ["codex"])]
    assert "Codex tab handle is unavailable: bad runtime" in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_run_replaces_process_with_configured_cli(tmp_path, monkeypatch, provider):
    booley_dir = tmp_path / "configured-project"
    loaded: list[tuple[Path, Path]] = []
    executed: list[tuple[str, list[str]]] = []
    config = type("Config", (), {"default_cli": provider})()

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
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)

    assert loaded == [(tmp_path, booley_dir)]
    assert executed == [(provider, ["S17", "-n", "S17"] if provider == "claude" else [provider])]


def test_run_reports_missing_configured_cli(tmp_path, monkeypatch, capsys):
    config = type("Config", (), {"default_cli": "codex"})()
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: config)
    monkeypatch.setattr(
        chat_cmd.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError),
    )

    assert chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path) == 2
    assert "configured agent CLI 'codex' was not found on PATH" in capsys.readouterr().err


def test_run_reports_invalid_project_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(
        chat_cmd,
        "load_agent_settings",
        lambda _root, *, booley_dir: (_ for _ in ()).throw(BackendConfigError("invalid provider")),
    )

    assert chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path) == 2
    assert "could not resolve the Project's agent provider" in capsys.readouterr().err


def test_run_does_not_mask_unexpected_runtime_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(
        chat_cmd,
        "load_agent_settings",
        lambda _root, *, booley_dir: (_ for _ in ()).throw(RuntimeError("backend defect")),
    )

    with pytest.raises(RuntimeError, match="backend defect"):
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)


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
        chat_cmd.run(argparse.Namespace(agent_args=[]), project_root)

    assert executed == [("codex", ["codex"])]


def test_worktree_provider_config_uses_the_shared_runtime_handle_map(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import reset_cache, resolve_common_booley_dir

    worktree = tmp_path / "worktree"
    local = worktree / ".booley"
    shared = tmp_path / "project" / ".booley"
    local.mkdir(parents=True)
    shared.mkdir(parents=True)
    (local / "booley.toml").write_text('[agent]\nprovider="codex"\n')
    (shared / "booley.toml").write_text('[agent]\nprovider="claude"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(shared))
    monkeypatch.setattr(chat_cmd, "resolve_common_booley_dir", resolve_common_booley_dir)
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
            chat_cmd.run(argparse.Namespace(agent_args=[]), worktree)
        assert directories == [shared]
        assert chat_cmd.get_agent_settings().default_cli == "codex"
    finally:
        reset_cache()


def _configure_claude(monkeypatch, provider="claude"):
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(
        chat_cmd, "get_agent_settings", type("Config", (), {"default_cli": provider})
    )


def test_claude_named_exec_preserves_codex_command(tmp_path, monkeypatch):
    _configure_claude(monkeypatch)
    reservations = []
    monkeypatch.setattr(
        chat_cmd,
        "reserve_session_handle",
        lambda directory, key: reservations.append((directory, key)) or "S17",
        raising=False,
    )
    executed = []

    def execute(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execute)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)
    assert executed == [("claude", ["S17", "-n", "S17"])], "Claude launch must use reserved handle"
    assert reservations == [
        (tmp_path, process_session_key(ProcessIdentity(98765, "fixture", 101)))
    ]
    monkeypatch.setattr(
        chat_cmd, "get_agent_settings", type("Config", (), {"default_cli": "codex"})
    )
    monkeypatch.setattr(
        chat_cmd.codex_chat,
        "command",
        lambda _directory: ("/native/codex", ["S23", "--no-daemon", "--fixture"]),
    )
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)
    assert executed[-1] == ("/native/codex", ["S23", "--no-daemon", "--fixture"])
    assert len(reservations) == 1


def test_claude_reservation_failure_falls_back(tmp_path, monkeypatch, capsys):
    for error in (
        BlockingIOError("busy"),
        PermissionError("unreadable"),
        ValueError("malformed"),
        OSError("save failed"),
    ):
        _check_claude_reservation_failure(tmp_path, monkeypatch, capsys, error)


def _check_claude_reservation_failure(tmp_path, monkeypatch, capsys, error):
    _configure_claude(monkeypatch)
    reservations = []

    def reserve(directory, key):
        reservations.append((directory, key))
        raise error

    monkeypatch.setattr(chat_cmd, "reserve_session_handle", reserve, raising=False)
    executed = []

    def execute(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execute)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)
    assert reservations == [
        (tmp_path, process_session_key(ProcessIdentity(98765, "fixture", 101)))
    ], "Claude launch must attempt reservation"
    assert executed == [("claude", ["claude"])]
    notes = [line for line in capsys.readouterr().err.splitlines() if line.startswith("NOTE:")]
    assert len(notes) == 1 and len(notes[0]) < 160
    assert "Claude" in notes[0] and str(error) in notes[0]


@pytest.mark.parametrize("failure", ["identity", "directory"])
def test_claude_missing_launch_identity_or_directory_is_advisory(
    tmp_path, monkeypatch, capsys, failure
):
    _configure_claude(monkeypatch)
    if failure == "identity":
        monkeypatch.setattr(chat_cmd, "capture_process_identity", lambda _pid: None, raising=False)
    else:
        monkeypatch.setattr(
            chat_cmd,
            "resolve_common_booley_dir",
            lambda _root: (_ for _ in ()).throw(ValueError("directory unavailable")),
        )

    def execute(executable, argv):
        assert (executable, argv) == ("claude", ["claude"])
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execute)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path)
    assert "NOTE: Claude" in capsys.readouterr().err


def test_claude_named_launch_missing_executable_keeps_existing_error(
    tmp_path, monkeypatch, capsys
):
    _configure_claude(monkeypatch)
    monkeypatch.setattr(
        chat_cmd.os, "execvp", lambda *_args: (_ for _ in ()).throw(FileNotFoundError)
    )
    assert chat_cmd.run(argparse.Namespace(agent_args=[]), tmp_path) == 2
    assert "configured agent CLI 'claude' was not found on PATH" in capsys.readouterr().err


def test_claude_worktree_settings_use_shared_runtime_reservation(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import reset_cache, resolve_common_booley_dir

    local = tmp_path / "worktree" / ".booley"
    shared = tmp_path / "main" / ".booley"
    local.mkdir(parents=True)
    shared.mkdir(parents=True)
    (local / "booley.toml").write_text('[agent]\nprovider="claude"\n')
    (shared / "booley.toml").write_text('[agent]\nprovider="codex"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(shared))
    monkeypatch.setattr(chat_cmd, "resolve_common_booley_dir", resolve_common_booley_dir)
    directories = []
    monkeypatch.setattr(
        chat_cmd,
        "reserve_session_handle",
        lambda directory, _key: directories.append(directory) or "S17",
        raising=False,
    )
    monkeypatch.setattr(
        chat_cmd.os, "execvp", lambda *_args: (_ for _ in ()).throw(_ExecCalledError)
    )
    reset_cache()
    try:
        with pytest.raises(_ExecCalledError):
            chat_cmd.run(argparse.Namespace(agent_args=[]), local.parent)
        assert directories == [shared]
        assert chat_cmd.get_agent_settings().default_cli == "claude"
    finally:
        reset_cache()


def _capture_exec(monkeypatch) -> list[tuple[str, list[str]]]:
    """Replace `os.execvp` with a recorder that stops the launch."""
    executed: list[tuple[str, list[str]]] = []

    def execute(executable, argv):
        executed.append((executable, argv))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execute)
    return executed


def _launch(tmp_path, agent_args):
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=agent_args), tmp_path)


def test_codex_launch_paths_append_agent_args(tmp_path, monkeypatch):
    _configure_claude(monkeypatch, "codex")
    executed = _capture_exec(monkeypatch)
    forwarded = ["resume", "--last"]
    native = ("/native/codex", ["S23", "--no-daemon"])
    npm = (
        "/usr/bin/node",
        ["/usr/bin/node", "/shim.mjs", "S23", "/lib/codex.js", "--no-daemon"],
    )
    for launch in (native, npm):
        monkeypatch.setattr(
            chat_cmd.codex_chat, "command", lambda _directory, launch=launch: launch
        )
        _launch(tmp_path, forwarded)
        assert executed[-1] == (launch[0], [*launch[1], *forwarded])
    monkeypatch.setattr(
        chat_cmd,
        "resolve_common_booley_dir",
        lambda _root: (_ for _ in ()).throw(ValueError("no dir")),
    )
    _launch(tmp_path, forwarded)
    assert executed[-1] == ("codex", ["codex", *forwarded])


def test_claude_launch_paths_append_agent_args(tmp_path, monkeypatch):
    _configure_claude(monkeypatch)
    executed = _capture_exec(monkeypatch)
    _launch(tmp_path, ["--resume", "abc"])
    assert executed[-1] == ("claude", ["S17", "-n", "S17", "--resume", "abc"])

    def unavailable(*_args):
        raise OSError("save failed")

    monkeypatch.setattr(chat_cmd, "reserve_session_handle", unavailable, raising=False)
    _launch(tmp_path, ["--resume", "abc"])
    assert executed[-1] == ("claude", ["claude", "--resume", "abc"])


def test_claude_user_name_replaces_booley_name(tmp_path, monkeypatch):
    _configure_claude(monkeypatch)
    executed = _capture_exec(monkeypatch)
    for agent_args in (["-n", "foo"], ["--name=foo"]):
        _launch(tmp_path, agent_args)
        assert executed[-1] == ("claude", ["S17", *agent_args])


def _configure_settings(monkeypatch, *, default_cli, clis=()):
    """Point ``run`` at in-memory settings (no Project config on disk)."""
    settings = AgentSettings(default_cli=default_cli, clis=tuple(clis))
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", lambda: settings)


def test_cli_flag_launches_allowed_cli(tmp_path, monkeypatch):
    _configure_settings(monkeypatch, default_cli="claude", clis=("claude", "codex"))
    executed = _capture_exec(monkeypatch)

    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=["resume"], cli="codex"), tmp_path)

    assert executed == [("codex", ["codex", "resume"])]


def test_cli_flag_rejects_cli_outside_allowed_set(tmp_path, monkeypatch, capsys):
    _configure_settings(monkeypatch, default_cli="claude")
    executed = _capture_exec(monkeypatch)

    assert chat_cmd.run(argparse.Namespace(agent_args=[], cli="codex"), tmp_path) == 2

    assert "Agent CLI 'codex' is not allowed in this Project; allowed: claude" in (
        capsys.readouterr().err
    )
    assert executed == []


def test_cli_flag_reads_allowed_set_from_project_config(tmp_path, monkeypatch):
    for name in ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_AGENT_APP"):
        monkeypatch.delenv(name, raising=False)
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n', encoding="utf-8"
    )
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda _root: booley_dir)
    executed = _capture_exec(monkeypatch)

    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[], cli="codex"), tmp_path)

    assert executed == [("codex", ["codex"])]


def test_no_cli_flag_launches_default_cli(tmp_path, monkeypatch):
    """No flag (or a namespace without one, as bare `booley` builds) opens the default CLI."""
    _configure_settings(monkeypatch, default_cli="codex", clis=("claude", "codex"))
    executed = _capture_exec(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["booley", "chat", "--last"])

    for args in (
        booley_cli._parse_cli(),
        argparse.Namespace(agent_args=["--last"]),
    ):
        with pytest.raises(_ExecCalledError):
            chat_cmd.run(args, tmp_path)

    assert executed == [("codex", ["codex", "--last"])] * 2
    assert booley_cli._parse_cli().cli is None


_GATEWAY_VALUES = {
    "BOOLEY_GATEWAY_URL": "https://gw.example/v1",
    "BOOLEY_GATEWAY_TOKEN": "SYNTHETIC_GATEWAY_TOKEN",
    "BOOLEY_GATEWAY_TOKEN_COMMAND": "print-token",
    "BOOLEY_GATEWAY_MODEL": "gpt-5.1",
    "BOOLEY_GATEWAY_EFFORT": "high",
}


def _gateway_sandbox(monkeypatch, *, record):
    """A container process with the gateway settings set; *record* is ``BOOLEY_AGENT_CLIS``."""
    from booley.config import agent as agent_config

    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    for name, value in _GATEWAY_VALUES.items():
        monkeypatch.setenv(name, value)
    # Register the marker for restoration: the launch writes it straight into os.environ.
    monkeypatch.setenv("BOOLEY_HIDDEN_GATEWAY", "unset-me")
    monkeypatch.delenv("BOOLEY_HIDDEN_GATEWAY")
    if record is None:
        monkeypatch.delenv("BOOLEY_AGENT_CLIS", raising=False)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    else:
        monkeypatch.setenv("BOOLEY_AGENT_CLIS", record)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")


def _exec_environment(tmp_path, monkeypatch) -> dict[str, str]:
    """``booley chat --cli claude`` and the environment it would exec with."""
    import os

    _configure_settings(monkeypatch, default_cli="claude", clis=("claude", "codex"))
    seen: dict[str, str] = {}

    def execute(_executable, _argv):
        seen.update(os.environ)
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execute)
    with pytest.raises(_ExecCalledError):
        chat_cmd.run(argparse.Namespace(agent_args=[], cli="claude"), tmp_path)
    return seen


def test_claude_chat_launch_drops_gateway_settings(tmp_path, monkeypatch):
    _gateway_sandbox(monkeypatch, record="claude=subscription,codex=gateway")

    seen = _exec_environment(tmp_path, monkeypatch)

    assert not [name for name in _GATEWAY_VALUES if name in seen]
    assert seen["BOOLEY_HIDDEN_GATEWAY"] == "1"


def test_handwritten_gateway_claude_chat_keeps_gateway_settings(tmp_path, monkeypatch):
    """Guard: a hand-written spec (no record) keeps today's Claude environment."""
    from booley.runtime import _claude_backend

    _gateway_sandbox(monkeypatch, record=None)

    seen = _exec_environment(tmp_path, monkeypatch)

    assert {name: seen[name] for name in _GATEWAY_VALUES} == _GATEWAY_VALUES
    assert "BOOLEY_HIDDEN_GATEWAY" not in seen
    overrides = _claude_backend._claude_env_overrides("subscription")
    assert not [name for name in _GATEWAY_VALUES if name in overrides]
