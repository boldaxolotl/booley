"""A checkout without a Booley Directory of its own never shares another's session store (#3483)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from booley.bwave.sessions import current_session_store, session_store
from tests.booley_checkouts_support import commit_all, init_repo, isolate_git


def test_git_checkout_without_a_booley_directory_uses_the_temporary_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_git(tmp_path, monkeypatch)
    checkout = tmp_path / "checkout"
    init_repo(checkout)
    (checkout / "README.md").write_text("fixture\n", encoding="utf-8")
    commit_all(checkout)
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temporary"))

    for store in (session_store(checkout), current_session_store(), session_store(None)):
        assert store.checkout is None
        assert store.path == tmp_path / "temporary" / "bwave" / "bwave_sessions.json"
    assert not (checkout / ".booley").exists()


def test_a_trace_outside_the_checkout_is_recorded_absolute(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".booley").mkdir(parents=True)
    store = session_store(checkout)
    inside = checkout / "sim" / "dut.fst"
    outside = tmp_path / "elsewhere" / "dut.fst"

    assert store.record(inside) == "sim/dut.fst"
    assert store.trace_path("sim/dut.fst") == checkout / "sim" / "dut.fst"
    assert store.record(outside) == str(outside)
    assert store.trace_path(str(outside)) == outside


def test_an_unresolvable_booley_directory_falls_back_to_the_temporary_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.bwave import sessions
    from booley.core.booley_layout import BooleyLayoutError

    def refuse(_checkout: Path) -> Path:
        raise BooleyLayoutError("unusable layout")

    monkeypatch.setattr(sessions, "resolve_authoritative_booley_dir", refuse)
    monkeypatch.setattr(sessions, "containing_checkout", lambda _cwd: tmp_path)

    assert session_store(tmp_path).checkout is None
    assert current_session_store().checkout is None


def test_a_working_directory_that_cannot_be_inspected_falls_back_to_the_temporary_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.bwave import sessions

    def unreadable(_cwd: Path) -> Path:
        raise PermissionError("cannot read the working directory")

    monkeypatch.setattr(sessions, "containing_checkout", unreadable)

    assert current_session_store().checkout is None
