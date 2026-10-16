"""Doctor explains retained orphan disk use without changing records."""
# ruff: noqa: F811 — pytest fixture imports intentionally share parameter names.

from types import SimpleNamespace

from booley.harness import doctor
from tests.goals.test_inventory import remove_checkout, snapshot, terminal
from tests.goals.test_store import repo  # noqa: F401 — shared fixture


def test_doctor_reports_terminal_orphan_bytes_and_preview(repo, monkeypatch):
    rec = terminal(repo)
    remove_checkout(repo)
    notes = []
    reporter = SimpleNamespace(
        note_=lambda text, fix="": notes.append(text + fix),
        skip_=notes.append,
        warn_=lambda *_: None,
    )
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    before = snapshot(repo["project"])
    doctor._check_goal_presence(repo["main"], repo["project"], reporter)
    text = "\n".join(notes)
    assert rec.id in text and "booley goal prune" in text, "orphan record diagnostic is missing"
    assert "bytes" in text and "total" in text
    assert snapshot(repo["project"]) == before

    from booley.goals import inventory
    from booley.goals.inventory import logical_bytes
    from booley.goals.paths import record_paths

    assert str(logical_bytes(record_paths(repo["project"], rec.id).root)) in text
    for observed in [("present", "missing"), ("missing", "present"), ("unavailable", "missing")]:
        notes.clear()
        monkeypatch.setattr(inventory, "presence", lambda *_, observed=observed: observed)
        doctor._check_retained_goal_records(repo["main"], repo["project"], reporter)
        assert "Terminal orphan Goal Record" not in "\n".join(notes)
    notes.clear()
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: False)
    doctor._check_retained_goal_records(repo["main"], repo["project"], reporter)
    assert "booley session enter -- booley doctor" in "\n".join(notes)
