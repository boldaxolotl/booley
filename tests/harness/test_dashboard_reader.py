"""The Dashboard reader joins visible sessions with their reported activity."""

from __future__ import annotations

from booley.harness.dashboard import model
from booley.mcp.session_registry import Attribution, SessionRegistry

SCOPE = "pid:[17]"


def test_reader_joins_each_visible_session_with_its_reported_activity(tmp_path, monkeypatch):
    import time

    from booley.mcp.session_registry import Attribution, SessionRegistry
    from booley.runtime.session_activity import UNKNOWN, WAITING_TOOL, WORKING
    from booley.runtime.timefmt import rfc3339_from_epoch
    from tests.conftest import write_activity_signal

    monkeypatch.setattr(model, "namespace", lambda: "pid:[17]")
    now = time.time()
    registry = SessionRegistry(tmp_path)
    for thread in ("fresh", "stale", "silent"):
        registry.upsert(
            Attribution(f"codex:{thread}", "thread", "/fixture", "repo:w", namespace="pid:[17]"),
            "sim",
            "completed",
            now=now,
        )
    write_activity_signal(
        tmp_path, "codex:fresh", WAITING_TOOL, rfc3339_from_epoch(now - 30), "sim"
    )
    # A busy state older than any tool call is no longer evidence of activity.
    write_activity_signal(tmp_path, "codex:stale", WORKING, rfc3339_from_epoch(now - 3 * 3600))
    write_activity_signal(tmp_path, "codex:unregistered", WORKING, rfc3339_from_epoch(now))
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    activity = model.DashboardReader(tmp_path, tmp_path).read().activity
    assert set(activity) == {"codex:fresh", "codex:stale", "codex:silent"}
    assert (activity["codex:fresh"].state, activity["codex:fresh"].tool) == (WAITING_TOOL, "sim")
    assert activity["codex:stale"].state == UNKNOWN
    assert activity["codex:silent"].state == UNKNOWN
    # Reading activity is observational: no file is created, changed or removed.
    assert before == {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }


def test_codex_session_end_hides_its_row_on_the_next_dashboard_read(tmp_path, monkeypatch):
    import time

    from tests.harness.test_session_activity_hook import hook

    monkeypatch.setattr(model, "namespace", lambda: SCOPE)
    now = time.time()
    registry = SessionRegistry(tmp_path)
    for thread in ("ended", "open"):
        registry.upsert(
            Attribution(f"codex:{thread}", "thread", "/fixture", "repo:w", namespace=SCOPE),
            "sim",
            "completed",
            now=now - 60,
        )
    reader = model.DashboardReader(tmp_path, tmp_path)
    assert {row.attribution.key for row in reader.read().sessions} == {"codex:ended", "codex:open"}
    payload = {"hook_event_name": "SessionEnd", "session_id": "ended"}
    assert hook.record("codex", payload, project=tmp_path, now=now)
    assert [row.attribution.key for row in reader.read().sessions] == ["codex:open"]
    assert registry.path("codex:ended").exists()  # Reading hides; maintenance deletes.
