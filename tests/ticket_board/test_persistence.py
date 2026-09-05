"""Crash-safety tests for Ticket control-plane persistence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.ticket_board import persistence


def test_write_once_never_exposes_partial_final_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "receipt.json"

    def interrupt(_source: Path, _destination: Path) -> None:
        raise OSError("link interrupted")

    monkeypatch.setattr(os, "link", interrupt)

    with pytest.raises(OSError, match="link interrupted"):
        persistence.atomic_write_once(destination, b"complete\n")
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
