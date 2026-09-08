"""Notification delivery is optional and must preserve lifecycle results."""

from unittest.mock import Mock

import pytest

from booley.ticket_board import notifications, operations
from tests.ticket_board.conftest import make_ticket_file


@pytest.mark.parametrize("setting", ["events = 42", "ntfy_topic = 42", "notifications = []"])
def test_bad_config_does_not_fail_block(tio, tmp_path, monkeypatch, setting):
    text = setting if setting.startswith("notifications") else "[notifications]\n" + setting
    (tmp_path / "booley.toml").write_text(text)
    make_ticket_file(tio, "active", "probe")
    monkeypatch.setattr(operations, "ntfy_send", notifications.ntfy_send)
    monkeypatch.setattr(notifications.subprocess, "Popen", Mock())
    assert operations.op_block(tio, "probe", "need input", "planning") is True
    assert tio.find_ticket("probe")["status"] == "blocked"


@pytest.mark.parametrize("content", [b'{"status": []}', b"\xff"])
def test_bad_digest_does_not_fail_review(tio, monkeypatch, content):
    make_ticket_file(tio, "active", "probe")
    log = tio.logs_dir / "probe" / ".runtime"
    (log / "triage-prep").mkdir(parents=True)
    (log / "booley_state.json").write_text("{}")
    (log / "triage-prep" / "manifest.json").write_bytes(content)
    monkeypatch.setattr(operations, "_prepare_handoff_snapshot", lambda *_a: True)
    entry = tio.find_ticket("probe")
    assert operations._handoff_to_review(tio, "probe", entry, "running", "summary", None)
    assert tio.find_ticket("probe")["status"] == "review"


def test_sender_uses_https_and_bounded_delivery(tmp_path, monkeypatch):
    (tmp_path / "booley.toml").write_text('[notifications]\nntfy_topic = "probe"')
    monkeypatch.delenv("NTFY_DISABLE", raising=False)
    send = Mock()
    monkeypatch.setattr(notifications.subprocess, "Popen", send)
    notifications.ntfy_send("Title\nheader", "body")
    args = send.call_args.args[0]
    assert "https://ntfy.sh/probe" in args
    assert 0 < float(args[args.index("--connect-timeout") + 1]) <= 10
    assert 0 < float(args[args.index("--max-time") + 1]) <= 30
    assert "Title: Title header" in args


@pytest.mark.parametrize("merge", [False, True])
def test_successful_completion_notifies(tio, monkeypatch, merge):
    make_ticket_file(tio, "review", "probe")
    policy = Mock(merge=merge)
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", policy, None)
    )
    monkeypatch.setattr(operations, "_finish_completed_ticket", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        operations,
        "_complete_with_merge",
        lambda *_a: operations._approve_transition(tio, "probe"),
    )
    send = Mock()
    monkeypatch.setattr(operations, "ntfy_send", send)
    assert operations.op_complete(tio, "probe")
    assert tio.find_ticket("probe")["status"] == "done"
    send.assert_called_once()
    assert send.call_args.args[0] == "DONE: probe"


def test_failed_completion_does_not_notify(tio, monkeypatch):
    make_ticket_file(tio, "review", "probe")
    monkeypatch.setattr(operations, "_prepare_completion_request", lambda *_a: None)
    send = Mock()
    monkeypatch.setattr(operations, "ntfy_send", send)
    assert not operations.op_complete(tio, "probe")
    send.assert_not_called()


@pytest.mark.parametrize("setting", ["events = []", 'events = ["blocked"]'])
def test_completion_respects_event_filter(tio, tmp_path, monkeypatch, setting):
    (tmp_path / "booley.toml").write_text("[notifications]\n" + setting)
    make_ticket_file(tio, "review", "probe")
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", Mock(merge=False), None)
    )
    monkeypatch.setattr(operations, "_finish_completed_ticket", lambda *_a, **_kw: None)
    send = Mock()
    monkeypatch.setattr(operations, "ntfy_send", send)
    assert operations.op_complete(tio, "probe")
    assert tio.find_ticket("probe")["status"] == "done"
    send.assert_not_called()


def test_completion_retry_does_not_repeat_notification(tio, monkeypatch):
    make_ticket_file(tio, "done", "probe")
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", Mock(merge=True), None)
    )
    monkeypatch.setattr(operations, "_complete_with_merge", lambda *_a: True)
    send = Mock()
    monkeypatch.setattr(operations, "ntfy_send", send)
    assert operations.op_complete(tio, "probe")
    send.assert_not_called()


@pytest.mark.parametrize("error", [FileNotFoundError("curl"), ValueError("embedded null byte")])
def test_delivery_failure_does_not_fail_block(tio, tmp_path, monkeypatch, error):
    (tmp_path / "booley.toml").write_text('[notifications]\nntfy_topic = "probe"')
    monkeypatch.delenv("NTFY_DISABLE", raising=False)
    make_ticket_file(tio, "active", "probe")
    send = Mock(side_effect=error)
    monkeypatch.setattr(notifications.subprocess, "Popen", send)
    monkeypatch.setattr(operations, "ntfy_send", notifications.ntfy_send)
    assert operations.op_block(tio, "probe", "need input", "planning")
    assert tio.find_ticket("probe")["status"] == "blocked"
    send.assert_called_once()


def test_disabled_delivery_does_not_spawn(tmp_path, monkeypatch):
    (tmp_path / "booley.toml").write_text('[notifications]\nntfy_topic = "probe"')
    monkeypatch.setenv("NTFY_DISABLE", "1")
    send = Mock()
    monkeypatch.setattr(notifications.subprocess, "Popen", send)
    notifications.ntfy_send("Title", "body")
    send.assert_not_called()


def test_missing_project_does_not_break_notifications(monkeypatch):
    from booley.runtime import project_dir

    monkeypatch.setattr(project_dir, "resolve_project_dir", Mock(side_effect=FileNotFoundError))
    send = Mock()
    monkeypatch.setattr(notifications.subprocess, "Popen", send)
    monkeypatch.delenv("NTFY_DISABLE", raising=False)
    notifications.ntfy_send("Title", "body")
    send.assert_not_called()


def test_failed_merge_does_not_notify(tio, monkeypatch):
    make_ticket_file(tio, "review", "probe")
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", Mock(merge=True), None)
    )
    monkeypatch.setattr(operations, "_complete_with_merge", lambda *_a: False)
    send = Mock()
    monkeypatch.setattr(operations, "ntfy_send", send)
    assert not operations.op_complete(tio, "probe")
    assert tio.find_ticket("probe")["status"] == "review"
    send.assert_not_called()


def test_automatic_done_handoff_notifies(tio, monkeypatch):
    make_ticket_file(tio, "active", "probe", {"on_success": {"destination": "done"}})
    log = tio.logs_dir / "probe" / "human-logs"
    log.mkdir(parents=True)
    (log / "run.log").write_text("Developer finished\n")
    monkeypatch.setattr(operations, "_validate_transitions_for_handoff", lambda *_a: True)
    monkeypatch.setattr(operations, "_prepare_handoff_snapshot", lambda *_a: True)
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", Mock(merge=False), None)
    )
    monkeypatch.setattr(operations, "_finish_completed_ticket", lambda *_a, **_kw: None)
    send = Mock()
    monkeypatch.setattr(operations, "ntfy_send", send)
    assert operations.op_handoff(tio, "probe")
    assert tio.find_ticket("probe")["status"] == "done"
    send.assert_called_once_with("DONE: probe", "Ticket completed")
