"""A Booley MCP server started for a Booley-launched agent writes no Session rows (#2437)."""

import pytest
from mcp.types import CallToolRequestParams

from booley.mcp import server, session_observer, session_peer, session_registry
from booley.mcp.session_registry import Attribution, SessionRegistry
from tests.mcp_tools.test_session_registry import IDENTITY, SCOPE
from tests.mcp_tools.test_session_requests import _sdk_session


def _lifetime(tmp_path, monkeypatch, *, nested):
    """The server's own lifetime (it decides write access from the nested marker)."""
    if nested:
        monkeypatch.setenv("BOOLEY_MCP_NESTED", "1")
    else:
        monkeypatch.delenv("BOOLEY_MCP_NESTED", raising=False)
    monkeypatch.setattr(session_peer, "peer_identity", lambda *_a, **_k: None)
    monkeypatch.setattr(session_registry, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(session_observer, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(session_registry, "_worktree_facts", lambda p: (p, "fixture", "main"))
    lifetime = server._McpLifetime(None, None)
    monkeypatch.setattr(lifetime.sessions, "_registry", lambda: SessionRegistry(tmp_path))
    lifetime.sessions.budget = 1
    return lifetime


def _seed(tmp_path, key, *, at):
    other = Attribution(key, "thread", str(tmp_path), "fixture", process=IDENTITY, namespace=SCOPE)
    SessionRegistry(tmp_path).upsert(other, "probe", "ok", now=at)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [True, False], ids=["nested", "top-level"])
async def test_nested_server_writes_no_session_rows(tmp_path, monkeypatch, nested):
    observer = _lifetime(tmp_path, monkeypatch, nested=nested).sessions
    async with _sdk_session(observer, "codex-mcp-client") as post:
        await post("tools/list", {"_meta": {"threadId": "t1"}})
        await post(
            "tools/call",
            {
                "name": "probe",
                "arguments": {"work_dir": str(tmp_path)},
                "_meta": {"threadId": "t1"},
            },
        )
    if observer._pending is not None:
        await observer._pending
    rows = SessionRegistry(tmp_path).snapshot().rows
    if nested:
        assert rows == ()
        assert not list((tmp_path / "runtime" / "sessions").glob("*.json"))
    else:
        assert [row.calls[-1].tool for row in rows] == ["probe"]


@pytest.mark.asyncio
async def test_nested_server_neither_writes_nor_prunes_but_still_warns(tmp_path, monkeypatch):
    observer = _lifetime(tmp_path, monkeypatch, nested=True).sessions
    _seed(tmp_path, "codex:other", at=observer.now())
    request = await server._observed_request_context(
        None,
        CallToolRequestParams(
            name="probe", arguments={"work_dir": str(tmp_path)}, _meta={"threadId": "mine"}
        ),
        observer,
    )
    if observer._pending is not None:
        await observer._pending
    # Reads still work: the caller sees the other session sharing its worktree.
    assert request.presentation_warning == "WARNING: another session shares this Goal worktree"
    registry = SessionRegistry(tmp_path)
    assert not registry.path("codex:mine").exists()
    # Maintenance is read-only too: an expired row is left for the interactive server.
    _seed(tmp_path, "codex:old", at=0)
    observer.now = lambda: 10.0**9
    await observer.maintain()
    assert registry.path("codex:old").exists()
