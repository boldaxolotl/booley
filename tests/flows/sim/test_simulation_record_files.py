"""Shared record file, JSON and digest helpers."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType

import pytest

from booley.flows.sim.coverage_json import content_digest


class _DomainError(Exception):
    """Stands in for a caller's own domain error type."""


def _simulation_record_files() -> ModuleType:
    return importlib.import_module("booley.flows.sim.record_files")


def test_document_and_evidence_digests_keep_their_newline_rules() -> None:
    record_files = _simulation_record_files()
    # A document digest drops the canonical trailing newline; an evidence digest keeps
    # every byte it is given.
    assert record_files.document_digest({"a": 1}) == (
        "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )
    assert content_digest(b'{"a":1}\n') == (
        "sha256:e346432021b04179518d9614f3560ccd71354a4ee101ddcb893d6959a9d6301c"
    )


def test_read_bounded_refuses_a_file_changed_during_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_files = _simulation_record_files()
    extended = tmp_path / "extended.json"
    extended.write_bytes(b'{"value":"original"}\n')

    # The file grows after the first chunk is read.
    real_read = os.read
    appended = False

    def read_then_extend(descriptor: int, count: int) -> bytes:
        nonlocal appended
        chunk = real_read(descriptor, count)
        if not appended:
            appended = True
            with extended.open("ab") as stream:
                stream.write(b"more\n")
        return chunk

    monkeypatch.setattr(os, "read", read_then_extend)
    with pytest.raises(_DomainError, match="changed during read"):
        record_files.read_bounded(extended, limit=1024, error=_DomainError)
