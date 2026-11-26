"""The managed Codex hook that blocks the T3 Code tools the Sandbox turns off (#5511)."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

DATA = Path(__file__).parents[2] / "src/booley/data/docker"
SCRIPT = DATA / "t3code_tool_deny_hook.py"
# Codex's names for ``t3-code`` and ``t3-code-direct``, and the form it takes when a second
# server sanitizes to the same name.
PREFIXES = ["mcp__t3_code__", "mcp__t3_code_direct__", "mcp__t3_code_0123456789ab__"]


def _denied() -> list[str]:
    return (DATA / "t3code-denied-tools.txt").read_text(encoding="utf-8").split()


def _run_hook(script: Path, tool_name: str, monkeypatch: pytest.MonkeyPatch) -> int:
    spec = importlib.util.spec_from_file_location("t3code_tool_deny_hook", script)
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    payload = json.dumps({"tool_name": tool_name}).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
    return hook.main()


@pytest.mark.parametrize("prefix", PREFIXES)
def test_hook_blocks_listed_t3code_tools(
    prefix: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in _denied():
        assert _run_hook(SCRIPT, prefix + name, monkeypatch) == 2
        assert name in capsys.readouterr().err

    kept = [prefix + "html_render", prefix + "t3_thread_wait"]
    for tool_name in [*kept, "mcp__booley__list_scheduled_tasks"]:
        assert _run_hook(SCRIPT, tool_name, monkeypatch) == 0
        assert capsys.readouterr().err == ""


def test_codex_policy_sends_both_t3code_servers_to_the_hook() -> None:
    policy = tomllib.loads((DATA / "codex-requirements.toml").read_text(encoding="utf-8"))
    (group,) = [
        group
        for group in policy["hooks"]["PreToolUse"]
        if any(SCRIPT.name in handler["command"] for handler in group["hooks"])
    ]
    matcher = re.compile(group["matcher"])

    for prefix in PREFIXES:
        assert all(matcher.search(prefix + name) for name in _denied())
    assert not matcher.search("mcp__booley__sim")


def test_hook_blocks_when_it_cannot_read_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Codex lets a call through when its hook fails any other way than exit 2 with a reason.
    alone = Path(shutil.copy(SCRIPT, tmp_path))

    assert _run_hook(alone, "mcp__t3_code__html_render", monkeypatch) == 2
    assert "t3code-denied-tools.txt" in capsys.readouterr().err
