"""Refusals and success paths of the shared campaign readers."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from types import MappingProxyType

import pytest

from booley.flows.sim import record_files
from booley.flows.sim.coverage_json import content_digest


class _DomainError(Exception):
    """Stands in for a caller's own domain error type."""


def test_read_bounded_returns_the_bytes_of_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "record.bin"
    path.write_bytes(b"abc")

    assert record_files.read_bounded(path, limit=3, error=_DomainError) == b"abc"
    assert record_files.stream_digest(path) == (3, content_digest(b"abc"))


def test_read_bounded_refuses_a_file_over_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "record.bin"
    path.write_bytes(b"abcd")

    with pytest.raises(_DomainError, match="size limit"):
        record_files.read_bounded(path, limit=3, error=_DomainError)


def test_read_bounded_refuses_a_file_that_grows_past_the_limit_while_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.bin"
    path.write_bytes(b"ab")
    real_read = os.read

    def read_extra(descriptor: int, count: int) -> bytes:
        return real_read(descriptor, count) or b"cd"

    monkeypatch.setattr(os, "read", read_extra)

    with pytest.raises(_DomainError, match="size limit"):
        record_files.read_bounded(path, limit=3, error=_DomainError)


def test_read_bounded_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(_DomainError, match="not a regular file"):
        record_files.read_bounded(tmp_path / "absent", limit=3, error=_DomainError)


def test_read_bounded_checks_single_link_only_when_asked(tmp_path: Path) -> None:
    path = tmp_path / "record.bin"
    path.write_bytes(b"abc")
    (tmp_path / "alias.bin").hardlink_to(path)

    assert record_files.read_bounded(path, limit=3, error=_DomainError) == b"abc"
    with pytest.raises(_DomainError, match="multiple filesystem links"):
        record_files.read_bounded(path, limit=3, error=_DomainError, single_link=True)


def test_canonical_json_refuses_a_value_that_is_not_json() -> None:
    with pytest.raises(ValueError, match="not JSON serializable"):
        record_files.canonical_json({"day": datetime.date(2026, 1, 1)})


def test_canonical_json_thaws_frozen_documents() -> None:
    frozen = MappingProxyType({"b": (1, 2), "a": MappingProxyType({"c": None})})

    assert record_files.canonical_json(frozen) == b'{"a":{"c":null},"b":[1,2]}\n'


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff", "not a JSON object"),
        (b'\xef\xbb\xbf{"a":1}\n', "not a JSON object"),
        (b"[]", "not a JSON object"),
        (b'{"b": 1}\n', "not canonical JSON"),
        (b'{"a":NaN}\n', "not canonical JSON"),
    ],
)
def test_parse_json_object_refuses_non_canonical_documents(raw: bytes, message: str) -> None:
    with pytest.raises(_DomainError, match=message):
        record_files.parse_json_object(raw, error=_DomainError, canonical=True)


def test_read_json_object_reads_a_canonical_document(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b'{"a":1}\n')

    assert record_files.read_json_object(path, limit=64, error=_DomainError, canonical=True) == {
        "a": 1
    }
