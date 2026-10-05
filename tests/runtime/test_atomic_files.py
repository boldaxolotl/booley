"""Crash-safety tests for the shared atomic file publication primitives."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.runtime import atomic_files
from booley.ticket_board import persistence


def test_ticket_persistence_reexports_the_shared_primitives() -> None:
    # The Ticket path is a compatibility re-export, not a second implementation.
    assert persistence.atomic_replace_bytes is atomic_files.atomic_replace_bytes
    assert persistence.atomic_write_once is atomic_files.atomic_write_once
    assert persistence.durable_unlink is atomic_files.durable_unlink
    assert persistence.WriteOnceConflictError is atomic_files.WriteOnceConflictError


def test_write_once_never_exposes_partial_final_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "receipt.json"

    def interrupt(_source: Path, _destination: Path) -> None:
        raise OSError("link interrupted")

    monkeypatch.setattr(os, "link", interrupt)

    with pytest.raises(OSError, match="link interrupted"):
        atomic_files.atomic_write_once(destination, b"complete\n")
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_once_accepts_identical_bytes_and_rejects_conflicts(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "receipt.json"

    assert atomic_files.atomic_write_once(destination, b"first\n") is True
    assert atomic_files.atomic_write_once(destination, b"first\n") is False
    with pytest.raises(atomic_files.WriteOnceConflictError, match="conflicting write-once"):
        atomic_files.atomic_write_once(destination, b"second\n")
    assert destination.read_bytes() == b"first\n"
    assert sorted(path.name for path in destination.parent.iterdir()) == ["receipt.json"]


def test_replace_bytes_publishes_complete_content(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"

    atomic_files.atomic_replace_bytes(destination, b"old\n")
    atomic_files.atomic_replace_bytes(destination, b"new\n")

    assert destination.read_bytes() == b"new\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_durable_unlink_reports_whether_the_path_existed(tmp_path: Path) -> None:
    destination = tmp_path / "claim.json"
    destination.write_bytes(b"claim\n")

    assert atomic_files.durable_unlink(destination) is True
    assert not destination.exists()
    assert atomic_files.durable_unlink(destination) is False
