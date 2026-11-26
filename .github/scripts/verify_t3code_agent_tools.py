"""Check which T3 Code tools Claude and Codex threads get in the Sandbox image (#4782, #5511).

CI runs this as the Sandbox user with no network inside ``bwave-smoke``. It starts ``t3 serve``
and one Claude and one Codex thread against a loopback fake model API, so the installed
``claude`` and ``codex`` read the image's managed policies. Each thread calls denied and kept
T3 Code tools. The check fails when

- T3 Code offers a tool that is neither in the shared denied list nor in ``_KEPT``, or a listed
  name that T3 Code no longer offers (``compare_tool_lists``): T3 Code changed its tools, so
  classify them;
- Claude's own tool list differs from ``_KEPT``;
- Claude's denied call is not refused with "No such tool available", or its kept call fails;
- Codex's denied call under ``t3-code`` is not blocked by the deny hook, its denied call under
  ``t3-code-direct`` is not refused or that server is not offered, or its kept call fails.

Claude hides the tools it denies, so its list cannot show a denied tool that T3 Code stopped
offering. The unfiltered list therefore comes from T3 Code's MCP server: a pass-through
``claude`` on ``PATH`` records the server T3 Code hands Claude, and the check asks it directly.

Standard library only: the image's Python runs it without Booley installed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any

from fake_model_api import FakeModelApi, call_outputs, tool_results
from verify_t3code_runtime import (
    command_output,
    fail,
    run_t3,
    start_server,
    stop_server,
    wait_until_ready,
)

_DENIED_TOOLS = "/usr/local/libexec/booley/t3code-denied-tools.txt"
_PREFIX = "mcp__t3-code__"
_DENIED_CALL = "list_scheduled_tasks"
_KEPT_CALL = "orchestrator_capabilities"
_COMMAND_TIMEOUT_S = 60
_THREAD_TIMEOUT_S = 300
_TEXT_LIMIT = 300
_MODEL = "claude-haiku-5-5"
# A model of the pinned Codex catalog: like its current models, it calls MCP tools from the
# JavaScript of Codex's ``exec`` tool, and only ``t3-code-direct``'s tools directly.
_CODEX_MODEL = "gpt-6.1-sol"
_DIRECT_NAMESPACE = "mcp__t3_code_direct"
# At the pinned T3 Code version, in a top-level thread with linked environments on and a
# project whose Git remote is a forge: every tool T3 Code offers, less the shared denials.
_KEPT_NAMES = """
ci_logs ci_rerun comment_on_issue create_issue create_threads delegate_task html_preview
html_render link_issue link_pull_request link_pull_requests list_issues list_thread_issues
list_thread_pull_requests orchestrator_capabilities pull_request_create pull_request_list
pull_request_merge pull_request_read pull_request_respond pull_request_review
pull_request_update preview_act preview_dialog preview_evaluate preview_navigate preview_open
preview_recording_start preview_recording_stop preview_resize preview_set_appearance
preview_snapshot preview_status preview_wait_for read_issue read_pull_request_guide send_to_user
set_issue_state t3_attachment_discard t3_attachment_prepare_upload t3_background_wait
t3_environment_preferences_update t3_environment_read t3_pending_request t3_preview_close
t3_preview_list t3_project_list t3_project_read t3_project_update t3_queue t3_theme_read
t3_theme_set t3_thread_configure t3_thread_interrupt t3_thread_launch t3_thread_list
t3_thread_organize t3_thread_read t3_thread_search t3_thread_send t3_thread_send_attachments
t3_thread_update t3_thread_wait t3_workflow_stop t3_worktree_handoff t3_worktree_list
t3_worktree_status task_cancel task_status unlink_issue unlink_pull_request
unlink_pull_requests unwatch_pull_request watch_pull_request write_pull_request_guides
"""
_KEPT = frozenset(_KEPT_NAMES.split())

_SHIM = """#!{python} -I
import os, subprocess, sys, threading
child = subprocess.Popen([{real!r}, *sys.argv[1:]], stdin=subprocess.PIPE)

def forward():
    with open({capture!r}, "ab") as capture:
        for line in sys.stdin.buffer:
            if b"mcp_set_servers" in line:
                capture.write(line)
                capture.flush()
            try:
                child.stdin.write(line)
                child.stdin.flush()
            except BrokenPipeError:
                break
    try:
        child.stdin.close()
    except BrokenPipeError:
        pass

# T3 Code keeps our stdin open until it sees us exit, so wait for Claude, not for stdin.
threading.Thread(target=forward, daemon=True).start()
os._exit(child.wait())
"""


def compare_tool_lists(
    offered: AbstractSet[str], denied: AbstractSet[str], kept: AbstractSet[str]
) -> list[str]:
    """Return one problem per offered tool on neither list and per listed tool not offered."""
    problems = [
        f"{name}: offered but neither denied nor kept" for name in sorted(offered - denied - kept)
    ]
    for kind, names in (("denied", denied), ("kept", kept)):
        problems += [f"{name}: {kind} but no longer offered" for name in sorted(names - offered)]
    return problems


def _write_claude_shim(bin_dir: Path, capture: Path) -> None:
    real = shutil.which("claude")
    if real is None:
        fail("claude is not installed", os.environ.get("PATH", ""))
    shim = bin_dir / "claude"
    shim.write_text(_SHIM.format(python=sys.executable, real=real, capture=str(capture)), "utf-8")
    shim.chmod(0o755)


def _t3_server(capture: Path) -> dict[str, Any]:
    lines = capture.read_text(encoding="utf-8").splitlines() if capture.exists() else []
    for line in lines:
        try:
            servers = json.loads(line)["request"]["servers"]
        except (ValueError, KeyError, TypeError):
            continue
        if "t3-code" in servers:
            return servers["t3-code"]
    return fail(
        "T3 Code did not hand Claude its MCP server", "(output withheld: it holds a token)"
    )


def _rpc(server: dict[str, Any], body: dict[str, Any], session: str | None) -> tuple[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **server.get("headers", {}),
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        server["url"], json.dumps(body).encode(), headers, method="POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=_COMMAND_TIMEOUT_S) as reply:
            return reply.headers.get("Mcp-Session-Id", ""), reply.read().decode("utf-8")
    except OSError as error:
        return fail("T3 Code's MCP server did not answer", type(error).__name__)


def _offered_tools(capture: Path) -> frozenset[str]:
    server = _t3_server(capture)
    client = {"name": "verify_t3code_agent_tools", "version": "0"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client},
    }
    session, _text = _rpc(server, initialize, None)
    _rpc(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    _session, text = _rpc(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    try:
        result = json.loads(text)["result"]
        names = frozenset(tool["name"] for tool in result["tools"])
    except (ValueError, KeyError, TypeError):
        fail("T3 Code's tools/list reply is not a tool list", text[:_TEXT_LIMIT])
    if "nextCursor" in result:
        fail("T3 Code's tools/list reply has more than one page", "the check reads the first page")
    return names


def _joined_text(content: object) -> str:
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def _judge_claude(model_api: FakeModelApi) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Return Claude's T3 Code tool names, the problems with them and the two call results."""
    shown = [
        sorted(
            tool["name"].removeprefix(_PREFIX)
            for tool in request["tools"]
            if tool["name"].startswith(_PREFIX)
        )
        for request in model_api.requests
        if request.get("tools")
    ]
    view = next((names for names in shown if names), [])
    results = max((tool_results(request) for request in model_api.requests), key=len, default=[])
    extra, missing = sorted(set(view) - _KEPT), sorted(_KEPT - set(view))
    problems: list[str] = []
    if extra or missing:
        problems.append(f"Claude's T3 Code tools: {extra} not kept, {missing} not shown")
    if len(results) != 2:
        return view, [*problems, f"Claude made {len(results)} tool calls; expected 2"], []
    denied, kept = (
        {
            "is_error": bool(result.get("is_error")),
            "text": _joined_text(result.get("content"))[:_TEXT_LIMIT],
        }
        for result in results
    )
    if not denied["is_error"] or "No such tool available" not in denied["text"]:
        problems.append(f"the denied tool {_DENIED_CALL} was not refused: {denied['text']}")
    if kept["is_error"]:
        problems.append(f"the kept tool {_KEPT_CALL} failed: {kept['text']}")
    return view, problems, [denied, kept]


def _codex_exec(tool: str) -> dict[str, Any]:
    code = f"text(await tools.mcp__t3_code__{tool}({{}}))"
    return {"type": "custom_tool_call", "name": "exec", "input": code}


def _codex_calls() -> list[dict[str, Any]]:
    """The denied call under each T3 Code server, then the kept call."""
    direct = {"type": "function_call", "namespace": _DIRECT_NAMESPACE, "name": _DENIED_CALL}
    return [_codex_exec(_DENIED_CALL), {**direct, "arguments": "{}"}, _codex_exec(_KEPT_CALL)]


def _judge_codex(model_api: FakeModelApi) -> tuple[list[str], list[str], dict[str, str]]:
    """Return the MCP servers Codex offered directly, the problems and the call outputs."""
    requests = model_api.responses_requests
    namespaces = sorted(
        {
            tool["name"]
            for request in requests
            for item in request.get("input", [])
            if item["type"] == "additional_tools"
            for tool in item["tools"]
            if tool["name"].startswith("mcp__")
        }
    )
    outputs = max((call_outputs(request) for request in requests), key=len, default=[])
    if len(outputs) != 3:
        return namespaces, [f"Codex made {len(outputs)} tool calls; expected 3"], {}
    denied, direct, kept = (_joined_text(item.get("output")) for item in outputs)
    problems: list[str] = []
    if _DIRECT_NAMESPACE not in namespaces:
        problems.append(f"Codex did not offer {_DIRECT_NAMESPACE}: {namespaces}")
    if f"turns off the T3 Code tool {_DENIED_CALL}" not in denied:
        problems.append(f"Codex ran or never reached the denied {_DENIED_CALL}: {denied}")
    if "unsupported call" not in direct:
        problems.append(f"the denied {_DENIED_CALL} was not refused directly: {direct}")
    if '"isError":false' not in kept:
        problems.append(f"Codex's kept tool {_KEPT_CALL} failed: {kept}")
    labels = ("t3-code denied", "t3-code-direct denied", "t3-code kept")
    texts = (text[:_TEXT_LIMIT] for text in (denied, direct, kept))
    return namespaces, problems, dict(zip(labels, texts, strict=True))


def _start_thread(root: Path, provider: str, model: str) -> str:
    """Run one thread of *provider* to its end and return the reply ``t3`` prints."""
    thread = run_t3(
        *["thread", "start", "--base-dir", str(root / "home"), "--provider", provider],
        *["--model", model, "--wait", "--json", str(root / "project"), "probe"],
        timeout_s=_THREAD_TIMEOUT_S,
    )
    if thread.returncode:
        fail(f"t3 thread start failed for {provider}", command_output(thread))
    return thread.stdout.strip()


def _run_threads(
    root: Path, model_api: FakeModelApi, capture: Path
) -> tuple[frozenset[str], dict[str, str]]:
    """Run a Claude and a Codex thread; return the tools T3 Code offers and the replies."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    _write_claude_shim(bin_dir, capture)
    model_api.codex_config(root / "codex-home")
    os.environ.update(
        {
            **model_api.claude_environment(),
            "CODEX_HOME": str(root / "codex-home"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            # Every MCP tool in the model request, none deferred behind tool search.
            "ENABLE_TOOL_SEARCH": "false",
        }
    )
    home, project, log = root / "home", root / "project", root / "serve.log"
    server = start_server(root, log)
    try:
        wait_until_ready(server, log, 120)
        added = run_t3("project", "add", "--base-dir", str(home), str(project))
        if added.returncode:
            fail("t3 project add failed", command_output(added))
        replies = {"claude": _start_thread(root, "claudeAgent", _MODEL)}
        offered = _offered_tools(capture)
        replies["codex"] = _start_thread(root, "codex", _CODEX_MODEL)
        return offered, replies
    finally:
        stop_server(server)


def _prepare_project(root: Path) -> None:
    # T3 Code hides its environment-link tools until this server setting is on, and its forge
    # tools until the project's Git remote is a forge it recognizes. Both are set so that the
    # check sees every tool T3 Code can offer a Claude thread.
    (root / "home/userdata").mkdir(parents=True)
    (root / "home/userdata/settings.json").write_text(
        json.dumps({"linkedEnvironmentsEnabled": True}), encoding="utf-8"
    )
    project = root / "project"
    project.mkdir()
    git = ["git", "-C", str(project)]
    subprocess.run([*git, "init", "-q"], check=True, timeout=_COMMAND_TIMEOUT_S)
    remote = "https://github.com/example/example.git"
    subprocess.run(
        [*git, "remote", "add", "origin", remote], check=True, timeout=_COMMAND_TIMEOUT_S
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--denied-tools", type=Path, default=Path(_DENIED_TOOLS))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    denied = frozenset(args.denied_tools.read_text(encoding="utf-8").split())
    calls = [(f"{_PREFIX}{_DENIED_CALL}", {}), (f"{_PREFIX}{_KEPT_CALL}", {})]
    with (
        tempfile.TemporaryDirectory(prefix="t3code-tools-") as tmp,
        FakeModelApi(calls, _codex_calls()) as api,
    ):
        root = Path(tmp)
        _prepare_project(root)
        offered, replies = _run_threads(root, api, root / "mcp-servers.jsonl")
        view, claude_problems, results = _judge_claude(api)
        namespaces, codex_problems, codex_outputs = _judge_codex(api)
    problems = compare_tool_lists(offered, denied, _KEPT) + claude_problems + codex_problems
    if problems:
        fail(
            "T3 Code's tools for Claude and Codex are not as the shared denied list says",
            "\n".join([*problems, f"offered: {sorted(offered)}"]),
        )
    evidence = {
        "offered": sorted(offered),
        "denied": sorted(denied),
        "kept": sorted(_KEPT),
        "claude_view": view,
        "calls": dict(zip((_DENIED_CALL, _KEPT_CALL), results, strict=True)),
        "codex_namespaces": namespaces,
        "codex_calls": codex_outputs,
        "thread_replies": replies,
    }
    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if args.evidence:
        args.evidence.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
