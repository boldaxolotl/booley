"""Retired configuration preserves local Ticket lifecycle behavior."""

from unittest.mock import Mock

import pytest

from booley.ticket_board import operations
from booley.ticket_board.board_layout import read_state_record, write_state_record
from booley.ticket_board.lifecycle import parse_board_target
from booley.ticket_board.workspace_ops import prepare_converted_ticket_baseline
from tests.ticket_board.conftest import publish_handoff_snapshot
from tests.ticket_board.test_v2_ticket_writes import _draft_document, _git_project


@pytest.fixture
def tio(tmp_path, monkeypatch):
    _, board = _git_project(tmp_path, monkeypatch)
    return board


def _make_ticket(tio, state, slug, *, destination="review"):
    document = _draft_document()
    if destination == "done":
        document = document.replace("on_success: [review]", "on_success: []")
    created = tio.create_ticket_document(slug, document)
    prepare_converted_ticket_baseline(tio._project_root, created, slug)
    assert tio.enqueue_ticket(slug)
    record = read_state_record(tio.tickets_dir, slug)
    write_state_record(tio.tickets_dir, slug, record.with_state(parse_board_target(state)))


@pytest.fixture(autouse=True)
def legacy_config(tio, monkeypatch):
    config = tio._project_root / ".booley_project" / "booley.toml"
    import subprocess

    popen = subprocess.Popen
    calls = []

    def record_delivery(*args, **kwargs):
        command = str(args)
        calls.append(command)
        assert "ntfy.sh" not in command, "retired notification delivery attempted"
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_delivery)
    config.write_text(
        config.read_text()
        + '\n[notifications]\nntfy_topic = "probe"\nevents = ["blocked", "review", "done"]\n'
    )

    yield
    assert not any("ntfy.sh" in call for call in calls)


@pytest.mark.parametrize("setting", ["events = 42", "ntfy_topic = 42", "notifications = []"])
def test_bad_config_does_not_fail_block(tio, tmp_path, monkeypatch, setting):
    text = setting if setting.startswith("notifications") else "[notifications]\n" + setting
    (tio._project_root / ".booley_project" / "booley.toml").write_text(text)
    _make_ticket(tio, "active", "probe")
    assert operations.op_block(tio, "probe", "need input", "planning") is True
    assert tio.find_ticket("probe")["status"] == "blocked"


@pytest.mark.parametrize("content", [b'{"status": []}', b"\xff"])
def test_malformed_triage_manifest_does_not_fail_review(tio, monkeypatch, content):
    _make_ticket(tio, "active", "probe")
    log = tio.logs_dir / "probe" / ".runtime"
    (log / "triage-prep").mkdir(parents=True)
    (log / "booley_state.json").write_text("{}")
    (log / "triage-prep" / "manifest.json").write_bytes(content)
    monkeypatch.setattr(operations, "_prepare_handoff_snapshot", publish_handoff_snapshot)
    entry = tio.find_ticket("probe")
    assert operations._handoff_to_review(tio, "probe", entry, "running", "summary", None)
    assert tio.find_ticket("probe")["status"] == "review"


@pytest.mark.parametrize("merge", [False, True])
def test_successful_completion(tio, monkeypatch, merge):
    _make_ticket(tio, "review", "probe")
    policy = Mock(merge=merge, cleanup=False)
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", policy, None)
    )
    monkeypatch.setattr(operations, "_finish_completed_ticket", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        operations,
        "_complete_with_merge",
        lambda *_a: operations._approve_transition(tio, "probe"),
    )
    assert operations.op_complete(tio, "probe")
    assert tio.find_ticket("probe")["status"] == "done"


def test_failed_completion_preserves_state(tio, monkeypatch):
    _make_ticket(tio, "review", "probe")
    monkeypatch.setattr(operations, "_prepare_completion_request", lambda *_a: None)
    assert not operations.op_complete(tio, "probe")


def test_completion_retry_preserves_done(tio, monkeypatch):
    _make_ticket(tio, "done", "probe")
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", Mock(merge=True), None)
    )
    monkeypatch.setattr(operations, "_complete_with_merge", lambda *_a: True)
    assert operations.op_complete(tio, "probe")


def test_failed_merge_preserves_state(tio, monkeypatch):
    _make_ticket(tio, "review", "probe")
    monkeypatch.setattr(
        operations, "_prepare_completion_request", lambda *_a: ("probe", Mock(merge=True), None)
    )
    monkeypatch.setattr(operations, "_complete_with_merge", lambda *_a: False)
    assert not operations.op_complete(tio, "probe")
    assert tio.find_ticket("probe")["status"] == "review"


def test_automatic_done_handoff(tio, monkeypatch):
    _make_ticket(tio, "active", "probe", destination="done")
    log = tio.logs_dir / "probe" / "human-logs"
    log.mkdir(parents=True, exist_ok=True)
    (log / "run.log").write_text("Developer finished\n")
    monkeypatch.setattr(operations, "_validate_transitions_for_handoff", lambda *_a: True)
    monkeypatch.setattr(operations, "_prepare_handoff_snapshot", publish_handoff_snapshot)
    monkeypatch.setattr(
        operations,
        "_prepare_completion_request",
        lambda *_a: ("probe", Mock(merge=False, cleanup=False), None),
    )
    monkeypatch.setattr(operations, "_finish_completed_ticket", lambda *_a, **_kw: True)
    assert operations.op_handoff(tio, "probe")
    assert tio.find_ticket("probe")["status"] == "done"
