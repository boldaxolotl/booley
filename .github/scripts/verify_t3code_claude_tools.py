"""Check which T3 Code tools a Claude thread gets in the Sandbox image (#4782).

CI runs this as the Sandbox user with no network inside ``bwave-smoke``. It starts ``t3 serve``
and one Claude thread against a loopback fake model API, so the installed ``claude`` reads the
image's managed settings. The thread calls one denied and one kept T3 Code tool. The check fails
when

- T3 Code offers a tool that is neither denied in the managed settings nor in ``_KEPT``, or a
  listed name that T3 Code no longer offers (``compare_tool_lists``): T3 Code changed its tools,
  so classify them;
- Claude's own tool list differs from ``_KEPT``;
- the denied call is not refused with "No such tool available", or the kept call fails.

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

from fake_model_api import FakeModelApi, tool_results
from verify_t3code_runtime import (
    command_output,
    fail,
    run_t3,
    start_server,
    stop_server,
    wait_until_ready,
)

_MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
_PREFIX = "mcp__t3-code__"
_DENIED_CALL = "list_scheduled_tasks"
_KEPT_CALL = "orchestrator_capabilities"
_COMMAND_TIMEOUT_S = 60
_THREAD_TIMEOUT_S = 300
_TEXT_LIMIT = 300
_MODEL = "claude-haiku-5-5"
# At the pinned T3 Code version, in a top-level thread with linked environments on and a
# project whose Git remote is a forge: every tool T3 Code offers, less the managed denials.
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


def _denied_tools(settings: Path) -> frozenset[str]:
    rules = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    return frozenset(rule.removeprefix(_PREFIX) for rule in rules if rule.startswith(_PREFIX))


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
    client = {"name": "verify_t3code_claude_tools", "version": "0"}
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


def _result_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)[:_TEXT_LIMIT]


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
        {"is_error": bool(result.get("is_error")), "text": _result_text(result)}
        for result in results
    )
    if not denied["is_error"] or "No such tool available" not in denied["text"]:
        problems.append(f"the denied tool {_DENIED_CALL} was not refused: {denied['text']}")
    if kept["is_error"]:
        problems.append(f"the kept tool {_KEPT_CALL} failed: {kept['text']}")
    return view, problems, [denied, kept]


def _run_thread(root: Path, model_api: FakeModelApi, capture: Path) -> tuple[frozenset[str], str]:
    """Start T3 Code and one Claude thread; return the tools T3 Code offers and its reply."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    _write_claude_shim(bin_dir, capture)
    os.environ.update(
        {
            **model_api.claude_environment(),
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
        thread = run_t3(
            *["thread", "start", "--base-dir", str(home), "--provider", "claudeAgent"],
            *["--model", _MODEL, "--wait", "--json", str(project), "probe"],
            timeout_s=_THREAD_TIMEOUT_S,
        )
        if thread.returncode:
            fail("t3 thread start failed", command_output(thread))
        return _offered_tools(capture), thread.stdout.strip()
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
    parser.add_argument("--managed-settings", type=Path, default=Path(_MANAGED_SETTINGS))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    denied = _denied_tools(args.managed_settings)
    calls = [(f"{_PREFIX}{_DENIED_CALL}", {}), (f"{_PREFIX}{_KEPT_CALL}", {})]
    with tempfile.TemporaryDirectory(prefix="t3code-tools-") as tmp, FakeModelApi(calls) as api:
        root = Path(tmp)
        _prepare_project(root)
        offered, reply = _run_thread(root, api, root / "mcp-servers.jsonl")
        view, claude_problems, results = _judge_claude(api)
    problems = compare_tool_lists(offered, denied, _KEPT) + claude_problems
    if problems:
        fail(
            "T3 Code's tools for Claude are not as the managed settings say",
            "\n".join([*problems, f"offered: {sorted(offered)}"]),
        )
    evidence = {
        "offered": sorted(offered),
        "denied": sorted(denied),
        "kept": sorted(_KEPT),
        "claude_view": view,
        "calls": dict(zip((_DENIED_CALL, _KEPT_CALL), results, strict=True)),
        "thread_reply": reply,
    }
    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if args.evidence:
        args.evidence.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
