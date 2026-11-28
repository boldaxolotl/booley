"""Codex launcher compatibility and npm shim contracts, without model calls."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness import codex_chat
from booley.runtime.handles import HandleStore
from booley.runtime.pid import ProcessIdentity
from tests.conftest import symlink_or_skip
from tests.subprocess_budget import run_bounded_child


@pytest.fixture
def installed(tmp_path, monkeypatch):
    executable = tmp_path / "@openai" / "codex" / "bin" / "codex.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env node\n")
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\n")  # Classification is by file name, never content.
    # Simulate the launcher platform without changing the host's file-lock backend.
    monkeypatch.setattr(codex_chat, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    monkeypatch.setattr(
        codex_chat.shutil,
        "which",
        lambda name: str(executable) if name == "codex" else str(node),
    )
    monkeypatch.setattr(
        codex_chat, "capture_process_identity", lambda _pid: ProcessIdentity(123, "fixture", 7)
    )
    monkeypatch.setattr(codex_chat, "resolve_common_booley_dir", lambda root: root)
    return executable


def test_npm_command_keeps_wrapper_and_reserves_handle(tmp_path, installed):
    project = tmp_path / "project"
    executable, argv, handle = codex_chat.command(project)
    assert executable == str(tmp_path / "node")
    assert handle == "S1"
    assert argv == [
        str(tmp_path / "node"),
        str(Path(codex_chat.__file__).parents[1] / "data/codex_chat.mjs"),
        "S1",
        str(installed),
        "--no-daemon",
    ]
    handles = HandleStore(project).load()
    assert handles.labels()["pid:fixture:123:7"] == "S1"
    assert handles.labels()["chat:codex:pid:fixture:123:7"] == "S1"
    assert codex_chat.command(project) == (executable, argv, "S1")
    assert HandleStore(project).load().next_number == 2


@pytest.mark.parametrize("name", ["codex", "codex-aarch64-unknown-linux-gnu"])
def test_native_command_keeps_exec_identity(tmp_path, installed, monkeypatch, name):
    native = installed.with_name(name)
    native.write_text("not an ELF file\n")
    launcher = tmp_path / "codex"
    symlink_or_skip(launcher, native)
    monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: str(launcher))
    assert codex_chat.command(tmp_path / "project") == (str(native), ["S1", "--no-daemon"], "S1")


def test_npm_update_is_discovered_on_each_launch(tmp_path, installed, monkeypatch):
    launcher = tmp_path / "codex"
    symlink_or_skip(launcher, installed)
    monkeypatch.setattr(
        codex_chat.shutil,
        "which",
        lambda name: str(launcher) if name == "codex" else str(tmp_path / "node"),
    )
    project = tmp_path / "project"
    assert codex_chat.command(project)[1][-2] == str(installed)
    updated = tmp_path / "updated" / "bin" / "codex.js"
    updated.parent.mkdir(parents=True)
    updated.write_text("#!/usr/bin/env node\n")
    launcher.unlink()
    symlink_or_skip(launcher, updated)
    assert codex_chat.command(project)[1][-2] == str(updated)
    assert HandleStore(project).load().next_number == 2


def test_session_name_titles_the_codex_tab(tmp_path, installed, monkeypatch):
    # Bug caught: the label never reaches the tab title (argv[0] natively, the shim argument
    # under npm), or a resumed tab without --name loses its stored label.
    native = installed.with_name("codex")
    native.write_text("not an ELF file\n")
    for launcher, title_at in (("npm", 2), ("native", 0)):
        if launcher == "native":
            monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: str(native))
        project = tmp_path / launcher
        _program, argv, handle = codex_chat.command(project, name="uart fix")
        assert handle == "S1"
        assert argv[title_at] == "S1 \u00b7 uart fix"
        assert argv[-1] == "--no-daemon"
        assert HandleStore(project).load().names == {"pid:fixture:123:7": "uart fix"}
        # The same launch process again, without --name: the stored label stays on the tab.
        assert codex_chat.command(project)[1][title_at] == "S1 \u00b7 uart fix"
        assert codex_chat.command(project, name="other")[1][title_at] == "S1 \u00b7 other"


def test_unsupported_codex_naming_notes_the_ignored_name(tmp_path, monkeypatch, capsys):
    # Bug caught: an explicit --name silently dropped where Codex cannot carry a title.
    monkeypatch.setattr(codex_chat, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    assert codex_chat.command(tmp_path, name="uart-fix") == ("codex", ["codex"], None)
    assert "NOTE: Codex cannot show --name here: Codex tab handles need Linux" in (
        capsys.readouterr().err
    )
    assert codex_chat.command(tmp_path) == ("codex", ["codex"], None)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "failure", ["script", "node-shim", "identity", "store", "lock", "node", "missing"]
)
def test_naming_failures_retain_plain_launch(tmp_path, installed, monkeypatch, failure):
    project = tmp_path / "project"
    if failure == "script":
        renamed = installed.with_name("codex-helper")
        installed.rename(renamed)
        monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: str(renamed))
    elif failure == "node-shim":
        node = tmp_path / "node"
        node.unlink()
        symlink_or_skip(node, tmp_path / "volta-shim")
    elif failure == "identity":
        monkeypatch.setattr(codex_chat, "capture_process_identity", lambda _pid: None)
    elif failure == "store":
        store = HandleStore(project)
        store.root.mkdir(parents=True)
        store.path.write_text("broken")
    elif failure == "lock":

        def locked(*_args, **_kwargs):
            raise BlockingIOError("session writer is busy")

        monkeypatch.setattr(codex_chat, "reserve_codex_chat", locked)
    elif failure == "node":
        monkeypatch.setattr(
            codex_chat.shutil, "which", lambda name: str(installed) if name == "codex" else None
        )
    else:
        monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: None)
    assert codex_chat.command(project) == ("codex", ["codex"], None)
    if failure != "store":
        assert not HandleStore(project).path.exists()


def test_non_linux_launch_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_chat, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    assert codex_chat.command(tmp_path) == ("codex", ["codex"], None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", [FileNotFoundError, ValueError, RuntimeError])
def test_runtime_directory_failure_keeps_plain_codex_launch(
    tmp_path, installed, monkeypatch, capsys, failure
):
    def unavailable(_root):
        raise failure("no runtime directory")

    monkeypatch.setattr(codex_chat, "resolve_common_booley_dir", unavailable)
    # Re-bind stderr now that capsys has replaced it.
    monkeypatch.setattr(codex_chat, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    project = tmp_path / "project"
    assert codex_chat.command(project) == ("codex", ["codex"], None)
    assert "NOTE: Codex tab handle is unavailable: no runtime directory" in capsys.readouterr().err
    assert not HandleStore(project).path.exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Booley runs the Codex chat Node shim only for Linux Codex launches",
)
def test_shim_preserves_arguments_environment_and_exit_status(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the npm shim")
    entrypoint = tmp_path / "wrapper.mjs"
    entrypoint.write_text(
        "console.log(JSON.stringify({title:process.title,argv:process.argv.slice(1),"
        "marker:process.env.BOOLEY_SHIM_TEST}));process.exit(17);"
    )
    monkeypatch.setenv("BOOLEY_SHIM_TEST", "inherited")
    shim = Path(codex_chat.__file__).parents[1] / "data/codex_chat.mjs"
    # The default bound leaves Node startup headroom on slow CI runners.
    result = run_bounded_child(
        [node, str(shim), "S123", str(entrypoint), "argument with spaces", "--no-daemon"],
    )
    assert result.returncode == 17
    assert json.loads(result.stdout) == {
        "title": "S123",
        "argv": [str(entrypoint), "argument with spaces", "--no-daemon"],
        "marker": "inherited",
    }


def test_strict_command_raises_instead_of_falling_back(tmp_path, installed, monkeypatch):
    # Bug caught: a worktree launch quietly starting an unnamed Codex tab.
    project = tmp_path / "project"
    monkeypatch.setattr(codex_chat, "capture_process_identity", lambda _pid: None)
    with pytest.raises(ValueError, match="launch process identity is unavailable"):
        codex_chat.command(project, strict=True)

    monkeypatch.setattr(codex_chat, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    with pytest.raises(codex_chat.NamingUnsupportedError, match="need Linux"):
        codex_chat.command(project, strict=True)
    assert not HandleStore(project).path.exists()
