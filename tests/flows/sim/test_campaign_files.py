"""Shared campaign file, JSON and digest helpers."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType

import pytest

from booley.flows.sim.coverage_json import content_digest


class _DomainError(Exception):
    """Stands in for a caller's own domain error type."""


def _campaign_files() -> ModuleType:
    return importlib.import_module("booley.flows.sim.campaign_files")


def test_document_and_evidence_digests_keep_their_newline_rules() -> None:
    campaign_files = _campaign_files()
    # A document digest drops the canonical trailing newline; an evidence digest keeps
    # every byte it is given.
    assert campaign_files.document_digest({"a": 1}) == (
        "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )
    assert content_digest(b'{"a":1}\n') == (
        "sha256:e346432021b04179518d9614f3560ccd71354a4ee101ddcb893d6959a9d6301c"
    )


def test_read_bounded_refuses_a_file_changed_during_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_files = _campaign_files()
    swapped = tmp_path / "swapped.json"
    extended = tmp_path / "extended.json"
    swapped.write_bytes(b'{"value":"original"}\n')
    extended.write_bytes(b'{"value":"original"}\n')

    # Extended: the file grows after the first chunk is read.
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

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", read_then_extend)
        with pytest.raises(_DomainError, match="changed during read"):
            campaign_files.read_bounded(extended, limit=1024, error=_DomainError)

    if os.name == "nt":  # Windows prevents renaming an open file.
        return

    # Swapped: the path is replaced by another file right after it is opened.
    real_open = campaign_files.open_regular_nofollow
    opened = False

    def open_then_replace(selected: Path, **kwargs: Path | None) -> int:
        nonlocal opened
        descriptor = real_open(selected, **kwargs)
        if not opened:
            opened = True
            selected.rename(selected.with_name("opened-original.json"))
            selected.write_bytes(b'{"value":"replacement"}\n')
        return descriptor

    monkeypatch.setattr(campaign_files, "open_regular_nofollow", open_then_replace)
    with pytest.raises(_DomainError, match="changed during read"):
        campaign_files.read_bounded(swapped, limit=1024, error=_DomainError)
