"""The standalone lifecycle hook script: agreement with the package, keys, never-fail contract."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.mcp.session_registry import SessionRegistry
from booley.runtime import chat_sessions
from booley.runtime import session_activity as activity
from booley.runtime.booley_dir import reset_cache, resolve_common_booley_dir
from booley.runtime.chat_sessions import reserve_codex_chat
from booley.runtime.handles import HandleStore
from booley.runtime.pid import capture_process_identity
from booley.runtime.session_activity import ActivityStore
from tests.booley_checkouts_support import (
    enter_worktree,
    isolate_git,
    main_project,
    paired_checkout,
    snapshot,
)

NOW = 1791454200.0
SCRIPT = Path(__file__).parents[2] / "src/booley/data/docker/session_activity_hook.py"
_SPEC = importlib.util.spec_from_file_location("session_activity_hook", SCRIPT)
hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hook)


def proc_tree(root, parents, commands=None):
    """Fake procfs: ``parents`` maps each PID to its parent PID."""
    for pid, parent in parents.items():
        directory = root / str(pid)
        (directory / "ns").mkdir(parents=True)
        (directory / "comm").write_text((commands or {}).get(pid, "sh") + "\n", encoding="utf-8")
        executable = (commands or {}).get(pid, "sh")
        (directory / "exe").symlink_to("/bin/" + executable)
        (directory / "cmdline").write_bytes(executable.encode() + b"\0")
        fields = ["S", str(parent), *["0"] * 17, str(1000 + pid)]
        (directory / "stat").write_text(f"{pid} (a b) c) " + " ".join(fields), encoding="utf-8")
        try:
            (directory / "ns" / "pid").symlink_to("pid:[4026531836]")
        except OSError:
            pytest.skip("symlinks unavailable on this platform")
    return root


def claude_key(proc, pid):
    return activity.process_session_key(capture_process_identity(pid, proc_root=proc))


# hook 500 <- sh 400 <- claude 300 <- shell 200 <- outer claude 100 <- init 1
CHAIN = {500: 400, 400: 300, 300: 200, 200: 100, 100: 1}


def test_claude_hook_resolves_the_owning_process_named_by_claude_pid(tmp_path):
    proc = proc_tree(tmp_path / "proc", CHAIN)
    project = tmp_path / "project"
    assert hook._ancestors(proc, 500) == [400, 300, 200, 100]
    payload = {"hook_event_name": "PreToolUse", "tool_name": "mcp__booley__sim"}
    assert hook.record(
        "claude",
        payload,
        project=project,
        now=NOW,
        env={"CLAUDE_PID": "300"},
        proc_root=proc,
        pid=500,
    )
    signal = ActivityStore(project).read(claude_key(proc, 300))
    assert (signal.state, signal.tool) == (activity.WAITING_TOOL, "mcp__booley__sim")
    assert signal.key == "pid:pid:[4026531836]:300:1300"
    # The outer session, an ancestor too, is not credited with the inner one's activity.
    assert ActivityStore(project).read(claude_key(proc, 100)) is None


@pytest.mark.parametrize("claimed", ["", "999", "not-a-pid", "500"])
def test_claude_hook_without_a_usable_claude_pid_uses_the_nearest_registered_ancestor(
    tmp_path, claimed
):
    proc = proc_tree(tmp_path / "proc", CHAIN)
    project = tmp_path / "project"
    payload = {"hook_event_name": "Stop"}
    env = {"CLAUDE_PID": claimed}
    # No ancestor is a known session: nothing is recorded, nothing is guessed.
    assert not hook.record(
        "claude", payload, project=project, now=NOW, env=env, proc_root=proc, pid=500
    )
    assert not ActivityStore(project).root.exists()
    registry = SessionRegistry(project, proc_root=proc)
    for pid in (300, 100):
        row = registry.path(claude_key(proc, pid))
        row.parent.mkdir(parents=True, exist_ok=True)
        row.write_text("{}", encoding="utf-8")
    assert hook.record(
        "claude", payload, project=project, now=NOW, env=env, proc_root=proc, pid=500
    )
    assert ActivityStore(project).read(claude_key(proc, 300)).state == activity.WAITING_USER
    assert ActivityStore(project).read(claude_key(proc, 100)) is None


def test_ancestor_walk_is_bounded_and_survives_unreadable_processes(tmp_path, monkeypatch):
    proc = proc_tree(tmp_path / "proc", {pid: pid - 1 for pid in range(3, 60)})
    monkeypatch.setattr(hook, "MAX_ANCESTORS", 4)
    assert hook._ancestors(proc, 59) == [58, 57, 56, 55]
    assert hook._ancestors(proc, 4) == [3, 2]  # PID 2 has no readable entry.
    assert hook._ancestors(proc, 7777) == []


def test_codex_hook_keys_by_thread_and_prefers_a_subagents_own_thread(tmp_path):
    def record(payload):
        return hook.record("codex", payload, project=tmp_path, now=NOW)

    store = ActivityStore(tmp_path)
    assert record({"hook_event_name": "UserPromptSubmit", "session_id": "root"})
    assert store.read("codex:root").state == activity.WORKING
    assert record({"hook_event_name": "PreToolUse", "session_id": "root", "agent_id": "child"})
    assert store.read("codex:child").state == activity.WAITING_TOOL
    assert store.read("codex:root").state == activity.WORKING
    for unusable in ({}, {"session_id": ""}, {"session_id": 7}, {"session_id": "a\nb"}):
        assert not record({"hook_event_name": "Stop", **unusable})
    assert not record({"session_id": "root"})  # No event name.
    assert not record({"hook_event_name": "SessionStart", "session_id": "root"})
    assert store.read("codex:root").state == activity.WORKING
    assert record({"hook_event_name": "SessionEnd", "session_id": "root"})
    assert store.read("codex:root") is None
    assert store.ended_at("codex:root", now=NOW + 5) is not None


@pytest.mark.parametrize("agent_id", ["child", "root"])
def test_launched_codex_activity_uses_tab_identity_and_ignores_subagent_stop(tmp_path, agent_id):
    proc = proc_tree(tmp_path / "proc", CHAIN, {300: "codex"})
    owner = capture_process_identity(300, proc_root=proc)
    reserve_codex_chat(tmp_path, owner)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "root"}
    assert hook.record("codex", payload, project=tmp_path, now=NOW, proc_root=proc, pid=500)
    store = ActivityStore(tmp_path)
    assert store.read(activity.process_session_key(owner)).state == activity.WORKING
    assert store.read("codex:root") is None
    assert not hook.record(
        "codex",
        {**payload, "hook_event_name": "Stop", "agent_id": agent_id},
        project=tmp_path,
        now=NOW,
        proc_root=proc,
        pid=500,
    )
    assert store.read(activity.process_session_key(owner)).state == activity.WORKING
    assert store.read("codex:child") is None
    assert not hook.record(
        "codex",
        {**payload, "hook_event_name": "PreToolUse", "agent_id": agent_id},
        project=tmp_path,
        now=NOW,
        proc_root=proc,
        pid=500,
    )
    assert not hook.record(
        "codex",
        {**payload, "hook_event_name": "SubagentStop", "agent_id": agent_id},
        project=tmp_path,
        now=NOW,
        proc_root=proc,
        pid=500,
    )
    assert hook.record(
        "codex",
        {**payload, "hook_event_name": "Stop"},
        project=tmp_path,
        now=NOW,
        proc_root=proc,
        pid=500,
    )
    assert store.read(activity.process_session_key(owner)).state == activity.WAITING_USER


def test_codex_activity_survives_broken_handle_map(tmp_path):
    path = tmp_path / "runtime/sessions/handles/map.json"
    path.parent.mkdir(parents=True)
    path.write_text("broken")
    assert hook.record(
        "codex",
        {"hook_event_name": "Stop", "session_id": "root"},
        project=tmp_path,
        now=NOW,
    )
    assert ActivityStore(tmp_path).read("codex:root").state == activity.WAITING_USER


@pytest.mark.parametrize("launcher", ["node", "codex"])
def test_nested_codex_stop_never_marks_the_outer_tab_idle(tmp_path, launcher):
    parents = {100: 1, 101: 100, 200: 101, 201: 200, 202: 201, 300: 101}
    commands = {100: launcher, 101: "codex" if launcher == "node" else "sh", 201: "codex"}
    proc = proc_tree(tmp_path / "proc", parents, commands)
    owner = capture_process_identity(100, proc_root=proc)
    reserve_codex_chat(tmp_path, owner)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "outer"}
    assert hook.record("codex", payload, tmp_path, NOW, proc_root=proc, pid=300)
    assert hook.record(
        "codex",
        {"hook_event_name": "Stop", "session_id": "nested"},
        tmp_path,
        NOW,
        proc_root=proc,
        pid=202,
    )
    store = ActivityStore(tmp_path)
    assert store.read(activity.process_session_key(owner)).state == activity.WORKING
    assert store.read("codex:nested").state == activity.WAITING_USER


def run_main(monkeypatch, capsys, argv, stdin, project=None):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    monkeypatch.setattr(hook, "resolve_common_booley_dir", lambda _env, _cwd: project)
    code = hook.main(argv)
    return code, capsys.readouterr()


def test_main_records_one_event_silently(tmp_path, monkeypatch, capsys):
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "t1"}).encode()
    code, output = run_main(monkeypatch, capsys, ["codex"], payload, tmp_path)
    assert (code, output.out, output.err) == (0, "", "")
    assert ActivityStore(tmp_path).read("codex:t1").state == activity.WAITING_USER


@pytest.mark.parametrize(
    ("argv", "stdin"),
    [
        ([], b"{}"),
        (["cursor"], b"{}"),
        (["codex", "extra"], b"{}"),
        (["codex"], b"not json"),
        (["codex"], b"[1, 2]"),
        (["codex"], b"\xff\xfe"),
    ],
)
def test_main_never_fails_the_turn_or_writes_to_stdout(tmp_path, monkeypatch, capsys, argv, stdin):
    code, output = run_main(monkeypatch, capsys, argv, stdin, tmp_path)
    assert (code, output.out) == (0, "")
    assert "not recorded" in output.err
    assert not ActivityStore(tmp_path).root.exists()


def test_main_ignores_oversized_payloads_and_directories_without_a_project(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(hook, "MAX_PAYLOAD_BYTES", 16)
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "t1"}).encode()
    code, output = run_main(monkeypatch, capsys, ["codex"], payload, tmp_path)
    assert (code, output.out) == (0, "")
    assert "size bound" in output.err
    monkeypatch.setattr(hook, "MAX_PAYLOAD_BYTES", 1 << 20)
    code, output = run_main(monkeypatch, capsys, ["codex"], payload)
    assert (code, output.out, output.err) == (0, "", "")


def test_main_reports_a_store_failure_without_failing(tmp_path, monkeypatch, capsys):
    blocked = tmp_path / "runtime"
    blocked.write_text("a file where the store directory belongs", encoding="utf-8")
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "t1"}).encode()
    code, output = run_main(monkeypatch, capsys, ["codex"], payload, tmp_path)
    assert (code, output.out) == (0, "")
    assert "not recorded" in output.err


def test_script_repeats_the_packages_event_table_states_and_bounds():
    assert hook.EVENT_STATES == activity.EVENT_STATES
    assert hook.END_EVENT == activity.END_EVENT
    assert hook.SCHEMA == activity.SCHEMA
    assert (hook.MAX_SIGNALS, hook.MAX_TOOL_NAME) == (activity.MAX_SIGNALS, activity.MAX_TOOL_NAME)
    assert hook.MAX_THREAD_ID == activity.MAX_THREAD_ID
    assert (hook.WORKING, hook.WAITING_TOOL, hook.WAITING_USER) == (
        activity.WORKING,
        activity.WAITING_TOOL,
        activity.WAITING_USER,
    )
    assert hook.BOOLEY_DIR_NAME == ".booley"
    assert hook.CODEX_NATIVE_NAMES == chat_sessions.CODEX_NATIVE_NAMES


@pytest.mark.parametrize(("event", "state"), sorted(activity.EVENT_STATES.items()))
def test_every_translated_event_is_readable_by_the_package(tmp_path, event, state):
    payload = {
        "hook_event_name": event,
        "session_id": "t",
        "tool_name": "mcp__booley__Sim",
        "notification_type": "idle_prompt",
    }
    assert hook.record("codex", payload, project=tmp_path, now=NOW + 0.9)
    signal = ActivityStore(tmp_path).read("codex:t")
    # Second-resolution UTC, truncated exactly as the package writes timestamps.
    assert (signal.state, signal.at) == (state, "2026-10-08T10:10:00Z")
    # Only a pending tool call names its tool.
    assert signal.tool == ("mcp__booley__Sim" if state == activity.WAITING_TOOL else "")
    assert activity.observed_activity(signal, now=NOW + 5).state == state


@pytest.mark.parametrize(
    ("tool", "stored"), [("bad\x1bname", ""), ({"not": "text"}, ""), ("t" * 500, "t" * 128)]
)
def test_untrusted_tool_names_are_dropped_or_bounded(tmp_path, tool, stored):
    payload = {"hook_event_name": "PreToolUse", "session_id": "t", "tool_name": tool}
    assert hook.record("codex", payload, project=tmp_path, now=NOW)
    assert ActivityStore(tmp_path).read("codex:t").tool == stored


def test_script_refuses_redirected_storage_and_a_full_store(tmp_path, monkeypatch):
    stop = {"hook_event_name": "Stop", "session_id": "a"}
    assert hook.record("codex", stop, project=tmp_path, now=NOW)
    monkeypatch.setattr(hook, "MAX_SIGNALS", 1)
    with pytest.raises(OSError, match="full"):
        hook.record("codex", {**stop, "session_id": "b"}, project=tmp_path, now=NOW)
    # A session that already has a signal can still update it.
    assert hook.record(
        "codex", {**stop, "hook_event_name": "PreToolUse"}, project=tmp_path, now=NOW
    )
    redirected = tmp_path / "redirected"
    (redirected / "runtime").mkdir(parents=True)
    try:
        (redirected / "runtime" / "session_activity").symlink_to(tmp_path / "runtime")
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(OSError, match="redirected"):
        hook.record("codex", stop, project=redirected, now=NOW)


def test_booley_dir_follows_booleys_own_resolution_order(tmp_path):
    checkout = tmp_path / "checkout"
    nested = checkout / "rtl" / "deep"
    nested.mkdir(parents=True)
    assert hook.resolve_booley_dir({}, nested) is None
    local = checkout / ".booley"
    local.mkdir()
    (local / "booley.toml").write_text("# Project\n")
    assert hook.resolve_booley_dir({}, nested) == local.resolve()
    elsewhere = tmp_path / "stealth"
    elsewhere.mkdir()
    (checkout / "booley.toml").write_text('[project]\ndir = "../stealth"\n', encoding="utf-8")
    assert hook.resolve_booley_dir({}, nested) == elsewhere.resolve()
    for unusable in ('[project]\ndir = "../missing"\n', "[project]\n", "not toml ["):
        (checkout / "booley.toml").write_text(unusable, encoding="utf-8")
        assert hook.resolve_booley_dir({}, nested) == local.resolve()
    env = {"BOOLEY_DIR": str(elsewhere)}
    assert hook.resolve_booley_dir(env, nested) == elsewhere
    assert hook.resolve_booley_dir({"BOOLEY_DIR": str(tmp_path / "gone")}, str(nested)) is None


def test_worktree_hook_writes_main_activity(tmp_path, monkeypatch):
    """Run as the image runs it, from a linked worktree with no BOOLEY_DIR (#2603)."""
    main, worktree = enter_worktree(tmp_path, monkeypatch)
    payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": "t1", "tool_name": "x"})
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "codex"],
        input=payload.encode(),
        capture_output=True,
        cwd=worktree,
        env={},
        timeout=30,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert ActivityStore(main).read("codex:t1").state == activity.WAITING_TOOL
    assert not (worktree / ".booley/runtime/session_activity").exists()


@pytest.mark.parametrize("topology", ["main", "snapshot", "paired", "relocated", "env-worktree"])
def test_hook_common_dir_matches_booley(tmp_path, monkeypatch, topology):
    """The stdlib mirror selects the directory ``resolve_common_booley_dir`` does."""
    isolate_git(tmp_path, monkeypatch)
    relocated = topology == "relocated"
    main = main_project(
        tmp_path,
        relative="hw/booley" if relocated else ".booley",
        toml='[project]\ndir = "hw/booley"\n' if relocated else "",
    )
    cwd = {
        "main": lambda: main,
        "snapshot": lambda: snapshot(main),
        "paired": lambda: paired_checkout(main),
        "relocated": lambda: snapshot(main, relative="hw/booley"),
        "env-worktree": lambda: snapshot(main),
    }[topology]
    cwd = cwd()
    env = {"BOOLEY_DIR": str(cwd / ".booley")} if topology == "env-worktree" else {}
    monkeypatch.chdir(cwd)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_cache()
    expected = (main / ("hw/booley" if relocated else ".booley")).resolve()
    assert hook.resolve_common_booley_dir(env, cwd) == expected
    assert resolve_common_booley_dir(cwd).resolve() == expected


def test_hook_keeps_selection_when_git_marker_is_malformed(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / ".booley").mkdir(parents=True)
    (checkout / ".booley/booley.toml").write_text("", encoding="utf-8")
    (checkout / ".git").write_text("not a pointer\n", encoding="utf-8")
    assert hook.resolve_common_booley_dir({}, checkout) == (checkout / ".booley").resolve()


def test_script_runs_standalone_without_site_packages_or_booley(tmp_path):
    """The image runs it with ``-I -S``: it must need nothing beyond the standard library."""
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text("# Project\n")
    payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": "t1", "tool_name": "x"})
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "codex"],
        input=payload.encode(),
        capture_output=True,
        cwd=tmp_path,
        env={},
        timeout=30,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert ActivityStore(project).read("codex:t1").state == activity.WAITING_TOOL
    source = SCRIPT.read_text(encoding="utf-8")
    assert "import booley" not in source
    assert "from booley" not in source


@pytest.mark.parametrize("kind", ["auth_success", "agent_completed", "", None])
def test_only_waiting_notifications_report_activity(tmp_path, kind):
    stop = {"hook_event_name": "UserPromptSubmit", "session_id": "t"}
    assert hook.record("codex", stop, project=tmp_path, now=NOW)
    other = {"hook_event_name": "Notification", "session_id": "t", "notification_type": kind}
    assert not hook.record("codex", other, project=tmp_path, now=NOW)
    assert ActivityStore(tmp_path).read("codex:t").state == activity.WORKING
    for waiting in sorted(hook.WAITING_NOTIFICATIONS):
        asking = {**other, "notification_type": waiting}
        assert hook.record("codex", asking, project=tmp_path, now=NOW)
        assert ActivityStore(tmp_path).read("codex:t").state == activity.WAITING_USER


def test_fallback_never_credits_an_outer_session_with_an_inner_clients_events(tmp_path):
    # The inner client (300) has not called Booley; only the outer session (100) has a row.
    proc = proc_tree(tmp_path / "proc", CHAIN, {300: "claude", 100: "claude"})
    project = tmp_path / "project"
    registry = SessionRegistry(project, proc_root=proc)
    row = registry.path(claude_key(proc, 100))
    row.parent.mkdir(parents=True)
    row.write_text("{}", encoding="utf-8")
    stop = {"hook_event_name": "Stop"}
    assert not hook.record(
        "claude", stop, project=project, now=NOW, env={}, proc_root=proc, pid=500
    )
    assert not ActivityStore(project).root.exists()
    # Once the inner client is itself a session, its own events are recorded.
    registry.path(claude_key(proc, 300)).write_text("{}", encoding="utf-8")
    assert hook.record("claude", stop, project=project, now=NOW, env={}, proc_root=proc, pid=500)
    assert ActivityStore(project).read(claude_key(proc, 300)).state == activity.WAITING_USER
    assert ActivityStore(project).read(claude_key(proc, 100)) is None


def test_main_survives_a_deeply_nested_payload(tmp_path, monkeypatch, capsys):
    depth = 200_000
    nested = (
        b'{"hook_event_name": "Stop", "session_id": "t1", "x": '
        + b"[" * depth
        + b"]" * depth
        + b"}"
    )
    assert len(nested) < hook.MAX_PAYLOAD_BYTES
    code, output = run_main(monkeypatch, capsys, ["codex"], nested, tmp_path)
    assert (code, output.out) == (0, "")
    assert "not recorded" in output.err


def test_hook_accepts_shape_valid_handle_maps(tmp_path):
    proc = proc_tree(tmp_path / "proc", CHAIN, {300: "codex"})
    owner = capture_process_identity(300, proc_root=proc)
    reserve_codex_chat(tmp_path, owner)
    store = HandleStore(tmp_path)
    data = json.loads(store.path.read_bytes())
    data["entries"].update(duplicate="S1", unusual="display")
    data["aliases"]["dangling"] = "missing"
    data["next_number"] = 0
    store.path.write_text(json.dumps(data))
    before = store.path.read_bytes()
    assert store.load().labels()[activity.process_session_key(owner)] == "S1"
    assert hook.record(
        "codex",
        {"hook_event_name": "Stop", "session_id": "root"},
        tmp_path,
        NOW,
        proc_root=proc,
        pid=500,
    )
    assert (
        ActivityStore(tmp_path).read(activity.process_session_key(owner)).state
        == activity.WAITING_USER
    )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_session_end_writes_the_end_marker_the_package_reads(tmp_path, client):
    assert getattr(hook, "ENDED", None) == getattr(activity, "ENDED", None) == "ended"
    project = tmp_path / "project"
    kwargs, key = {}, "codex:t"
    if client == "claude":
        proc = proc_tree(tmp_path / "proc", CHAIN)
        kwargs = {"env": {"CLAUDE_PID": "300"}, "proc_root": proc, "pid": 500}
        key = claude_key(proc, 300)
    payload = {"hook_event_name": "SessionEnd", "session_id": "t"}
    assert hook.record(client, payload, project=project, now=NOW + 0.9, **kwargs)
    store = ActivityStore(project)
    assert json.loads(store.path(key).read_text(encoding="utf-8")) == {
        "schema": activity.SCHEMA,
        "key": key,
        "state": "ended",
        "at": "2026-10-08T10:10:00Z",
    }
    assert store.read(key) is None  # An end marker is not activity.
    assert store.ended_at(key, now=NOW + 5) == "2026-10-08T10:10:00Z"
    # Ending is revocable: any later hook event of the session overwrites the marker.
    later = {"hook_event_name": "UserPromptSubmit", "session_id": "t"}
    assert hook.record(client, later, project=project, now=NOW + 2, **kwargs)
    assert store.ended_at(key, now=NOW + 5) is None
    assert store.read(key).state == activity.WORKING


def test_a_stop_that_leaves_background_work_waits_for_it_not_for_the_user(tmp_path):
    """Claude Code resumes the turn itself when a backgrounded MCP call returns."""
    proc = proc_tree(tmp_path / "proc", CHAIN)
    project = tmp_path / "project"
    key = claude_key(proc, 300)

    def record(payload, at):
        env = {"CLAUDE_PID": "300"}
        return hook.record("claude", payload, project, at, env=env, proc_root=proc, pid=500)

    reviewer = {"id": "k1", "type": "MCP task", "status": "running", "tool": "reviewer"}
    stop = {"hook_event_name": "Stop", "background_tasks": [reviewer]}
    assert record(stop, NOW)
    signal = ActivityStore(project).read(key)
    assert (signal.state, signal.tool) == (activity.WAITING_TOOL, "reviewer")
    # The idle prompt ~60 s after the turn ended is not a question to the user.
    idle = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}
    assert not record(idle, NOW + 60)
    assert ActivityStore(project).read(key).state == activity.WAITING_TOOL
    # A Stop with nothing awaited left in flight, then idle, does wait for the user.
    assert record({**stop, "background_tasks": []}, NOW + 61)
    assert record(idle, NOW + 121)
    assert ActivityStore(project).read(key).state == activity.WAITING_USER


@pytest.mark.parametrize(
    ("tasks", "state", "label"),
    [
        (
            [{"type": "MCP task", "tool": "reviewer"}, {"type": "shell"}, {"type": "subagent"}],
            activity.WAITING_TOOL,
            "reviewer +1",
        ),
        ([{"type": "MCP task", "tool": "bad\x1bname"}], activity.WAITING_TOOL, "MCP task"),
        ([{"type": "MCP task", "tool": ""}], activity.WAITING_TOOL, "MCP task"),
        # A dev server or log watcher may outlive the session; it is not awaited.
        (
            [{"type": "shell", "command": "npm run dev"}, {"type": "monitor"}],
            activity.WAITING_USER,
            "",
        ),
        (["not an object"], activity.WAITING_USER, ""),
    ],
)
def test_only_awaited_background_work_defers_the_user(tmp_path, tasks, state, label):
    stop = {"hook_event_name": "Stop", "session_id": "t", "background_tasks": tasks}
    assert hook.record("codex", stop, project=tmp_path, now=NOW)
    signal = ActivityStore(tmp_path).read("codex:t")
    assert (signal.state, signal.tool) == (state, label)


def test_a_question_replaces_a_background_wait(tmp_path):
    awaited = [{"type": "subagent"}]
    stop = {"hook_event_name": "Stop", "session_id": "t", "background_tasks": awaited}
    assert hook.record("codex", stop, project=tmp_path, now=NOW)
    asking = {"hook_event_name": "PermissionRequest", "session_id": "t", "tool_name": "Bash"}
    assert hook.record("codex", asking, project=tmp_path, now=NOW + 1)
    assert ActivityStore(tmp_path).read("codex:t").state == activity.WAITING_USER
