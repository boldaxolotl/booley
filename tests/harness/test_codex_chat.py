"""Codex launcher compatibility and npm shim contracts, without model calls."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness import codex_chat
from booley.runtime.handles import HandleStore
from booley.runtime.pid import ProcessIdentity
from tests.conftest import symlink_or_skip


@pytest.fixture
def installed(tmp_path, monkeypatch):
    executable = tmp_path / "@openai" / "codex" / "bin" / "codex.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env node\n")
    (executable.parent.parent / "package.json").write_text('{"name":"@openai/codex"}')
    node = tmp_path / "node"
    node.write_bytes(b"\x7fELF")
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
    return executable


def test_npm_command_keeps_wrapper_and_reserves_handle(tmp_path, installed):
    project = tmp_path / "project"
    executable, argv = codex_chat.command(project)
    assert executable == str(tmp_path / "node")
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
    assert codex_chat.command(project) == (executable, argv)
    assert HandleStore(project).load().next_number == 2


@pytest.mark.parametrize(
    "name",
    [
        "codex",
        "codex-x86_64-unknown-linux-musl",
        "codex-aarch64-unknown-linux-musl",
        "codex-x86_64-unknown-linux-gnu",
        "codex-aarch64-unknown-linux-gnu",
    ],
)
def test_native_command_keeps_exec_identity(tmp_path, installed, monkeypatch, name):
    native = installed.with_name(name)
    native.write_bytes(b"\x7fELF")
    launcher = tmp_path / "codex"
    symlink_or_skip(launcher, native)
    monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: str(launcher))
    assert codex_chat.command(tmp_path / "project") == (str(native), ["S1", "--no-daemon"])


@pytest.mark.parametrize("name", ["custom", "codex.js", "codex-helper", "codex-0.162.0"])
def test_unrecognized_native_names_do_not_reserve_unusable_handles(
    tmp_path, installed, monkeypatch, name
):
    native = installed.with_name(name)
    native.write_bytes(b"\x7fELF")
    monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: str(native))
    project = tmp_path / "project"
    assert codex_chat.command(project) == ("codex", ["codex"])
    assert not HandleStore(project).path.exists()


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
    (updated.parent.parent / "package.json").write_text('{"name":"@openai/codex"}')
    launcher.unlink()
    symlink_or_skip(launcher, updated)
    assert codex_chat.command(project)[1][-2] == str(updated)
    assert HandleStore(project).load().next_number == 2


@pytest.mark.parametrize(
    "failure", ["script", "package", "identity", "store", "lock", "node", "missing"]
)
def test_naming_failures_retain_plain_launch(tmp_path, installed, monkeypatch, failure):
    project = tmp_path / "project"
    if failure == "script":
        renamed = installed.with_name("custom")
        installed.rename(renamed)
        monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: str(renamed))
    elif failure == "package":
        (installed.parent.parent / "package.json").write_text('{"name":"another-package"}')
    elif failure == "identity":
        monkeypatch.setattr(codex_chat, "capture_process_identity", lambda _pid: None)
    elif failure == "store":
        store = HandleStore(project)
        store.root.mkdir(parents=True)
        store.path.write_text("broken")
    elif failure == "lock":

        def locked(*_args):
            raise BlockingIOError("session writer is busy")

        monkeypatch.setattr(codex_chat, "reserve_codex_chat", locked)
    elif failure == "node":
        monkeypatch.setattr(
            codex_chat.shutil, "which", lambda name: str(installed) if name == "codex" else None
        )
    else:
        monkeypatch.setattr(codex_chat.shutil, "which", lambda _name: None)
    assert codex_chat.command(project) == ("codex", ["codex"])
    if failure != "store":
        assert not HandleStore(project).path.exists()


def test_non_linux_launch_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_chat, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    assert codex_chat.command(tmp_path) == ("codex", ["codex"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("shim_kind", ["script", "symlink"])
def test_node_shims_fall_back_before_reserving_a_tab(tmp_path, installed, shim_kind):
    node = tmp_path / "node"
    node.unlink()
    if shim_kind == "script":
        node.write_text('#!/bin/sh\nexec /usr/bin/node "$@"\n')
    else:
        target = tmp_path / "volta-shim"
        target.write_bytes(b"\x7fELF")
        symlink_or_skip(node, target)
    project = tmp_path / "project"
    assert codex_chat.command(project) == ("codex", ["codex"])
    assert not HandleStore(project).path.exists()


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
    result = subprocess.run(
        [node, str(shim), "S123", str(entrypoint), "argument with spaces", "--no-daemon"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 17
    assert json.loads(result.stdout) == {
        "title": "S123",
        "argv": [str(entrypoint), "argument with spaces", "--no-daemon"],
        "marker": "inherited",
    }
