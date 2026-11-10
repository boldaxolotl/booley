"""Native Codex filenames agree across tab launch, registry and lifecycle hooks."""

import pytest
from tests.conftest import symlink_or_skip
from tests.harness.test_session_activity_hook import NOW, hook
from tests.runtime.test_chat_sessions import SCOPE, process, publish

from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime import chat_sessions
from booley.runtime import session_activity as activity
from booley.runtime.handles import HandleStore
from booley.runtime.session_activity import ActivityStore


@pytest.mark.parametrize("launcher", ["native", "node"])
def test_native_tabs_and_helpers_own_one_handle_but_nested_clients_do_not(tmp_path, launcher):
    native_name = "codex-x86_64-unknown-linux-musl"
    proc = tmp_path / "proc"
    owner = process(proc, 100, 1, executable="node" if launcher == "node" else native_name)
    main = process(proc, 101, 100, executable=native_name) if launcher == "node" else owner
    if launcher == "native":
        (proc / str(main.pid) / "cmdline").write_bytes(b"S1\0--no-daemon\0")
    backend = process(proc, 102, main.pid, executable=native_name, args=("app-server",))
    process(proc, 200, backend.pid, executable="sh")
    nested = process(proc, 201, 200, executable=native_name, args=("exec", "nested task"))
    process(proc, 202, nested.pid, executable="python3")
    process(proc, 300, backend.pid, executable="python3")
    chat_sessions.reserve_codex_chat(tmp_path, owner)
    registry = SessionRegistry(tmp_path, proc_root=proc)
    for peer in (main, backend):
        facts = Attribution("codex:main", "thread", "", "", process=peer, namespace=SCOPE)
        assert publish(registry, facts, "goal_status", NOW).key == activity.process_session_key(
            owner
        )
    nested_facts = Attribution("codex:nested", "thread", "", "", process=nested, namespace=SCOPE)
    assert publish(registry, nested_facts, "goal_status", NOW) == nested_facts
    assert len(registry.snapshot().rows) == 2
    assert HandleStore(tmp_path).load().labels()["codex:main"] == "S1"
    assert HandleStore(tmp_path).load().labels()["codex:nested"] == "S2"
    assert hook.record(
        "codex",
        {"hook_event_name": "UserPromptSubmit", "session_id": "main"},
        tmp_path,
        NOW,
        proc_root=proc,
        pid=300,
    )
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


@pytest.mark.parametrize("redirect", ["project", "runtime"])
def test_registry_rejects_redirected_handle_map_ancestors(tmp_path, redirect):
    project = tmp_path / "project"
    owner = process(tmp_path / "proc", 100, 1)
    chat_sessions.reserve_codex_chat(project, owner)
    # "project": a link above the Booley Directory (the entry itself may be a link).
    target = tmp_path if redirect == "project" else project / "runtime"
    link = tmp_path / "redirected"
    symlink_or_skip(link, target)
    selected = link / "project" if redirect == "project" else tmp_path / "alias"
    if redirect == "runtime":
        selected.mkdir()
        symlink_or_skip(selected / "runtime", link)
    with pytest.raises(OSError):
        HandleStore(selected).load()
    with pytest.raises(ValueError, match="redirected"):
        hook._handle_map(selected)


def test_hook_follows_symlinked_booley_dir_entry(tmp_path):
    """The Booley Directory entry may be a link (``/booley-project`` alias, host ``.booley``)."""
    proc = tmp_path / "proc"
    owner = process(proc, 100, 1, executable="codex")
    (proc / "100" / "cmdline").write_bytes(b"S1\0--no-daemon\0")
    backend = process(proc, 102, owner.pid, executable="codex", args=("app-server",))
    process(proc, 300, backend.pid, executable="python3")
    real = tmp_path / "real-booley-dir"
    chat_sessions.reserve_codex_chat(real, owner)
    entry = tmp_path / "project" / ".booley"
    entry.parent.mkdir()
    symlink_or_skip(entry, real)

    assert hook.record(
        "codex",
        {"hook_event_name": "UserPromptSubmit", "session_id": "main"},
        entry,
        NOW,
        proc_root=proc,
        pid=300,
    )

    # Attributed to the reserved chat, not the ``codex:<thread>`` fallback key.
    store = ActivityStore(real)
    assert store.read("codex:main") is None
    signal = store.read(activity.process_session_key(owner))
    assert signal is not None
    assert signal.state == activity.WORKING
