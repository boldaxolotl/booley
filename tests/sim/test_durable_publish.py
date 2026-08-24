"""Durability and failure contracts for trace artifact publication."""

from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.sim.durable_publish import publish_durable


def test_publish_replaces_destination_and_reports_each_phase(tmp_path: Path) -> None:
    source = tmp_path / "cache" / "trace.fst"
    destination = tmp_path / "project" / "trace.fst"
    source.parent.mkdir()
    destination.parent.mkdir()
    source.write_bytes(b"new trace" * 1024)
    destination.write_bytes(b"old trace")

    metrics = publish_durable(source, destination)

    assert destination.read_bytes() == source.read_bytes()
    assert source.exists()
    assert metrics.source_bytes == source.stat().st_size
    assert metrics.destination_free_bytes >= metrics.source_bytes
    assert metrics.copy_seconds >= 0
    assert metrics.file_sync_seconds >= 0
    assert metrics.directory_sync_seconds >= 0
    assert metrics.total_seconds >= sum(
        (metrics.copy_seconds, metrics.file_sync_seconds, metrics.directory_sync_seconds)
    )
    assert not list(destination.parent.glob(".trace.fst.*.tmp"))


def test_publish_syncs_file_before_replace_and_directory_afterward(tmp_path: Path) -> None:
    source = tmp_path / "source.fst"
    destination = tmp_path / "project" / "trace.fst"
    source.write_bytes(b"trace data")
    events: list[str] = []
    real_replace = Path.replace

    def record_replace(old: Path, new: Path) -> None:
        events.append("replace")
        real_replace(old, new)

    with (
        patch(
            "booley.sim.durable_publish._sync_file",
            side_effect=lambda _file: events.append("file"),
        ),
        patch(
            "booley.sim.durable_publish._sync_directory",
            side_effect=lambda _path: events.append("directory"),
        ),
        patch("booley.sim.durable_publish._replace_atomically", side_effect=record_replace),
    ):
        publish_durable(source, destination)

    assert events == ["file", "replace", "directory"]


def test_publish_failure_keeps_cache_and_previous_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.fst"
    destination = tmp_path / "project" / "trace.fst"
    source.write_bytes(b"new trace")
    destination.parent.mkdir()
    destination.write_bytes(b"old trace")

    with (
        patch(
            "booley.sim.durable_publish._sync_file",
            side_effect=OSError(errno.EIO, "sync failed"),
        ),
        pytest.raises(OSError, match="sync failed"),
    ):
        publish_durable(source, destination)

    assert source.read_bytes() == b"new trace"
    assert destination.read_bytes() == b"old trace"
    assert not list(destination.parent.glob(".trace.fst.*.tmp"))


def test_publish_rejects_insufficient_destination_space(tmp_path: Path) -> None:
    source = tmp_path / "source.fst"
    destination = tmp_path / "project" / "trace.fst"
    source.write_bytes(b"trace data")

    usage = SimpleNamespace(total=100, used=100, free=0)
    with (
        patch("booley.sim.durable_publish.shutil.disk_usage", return_value=usage),
        pytest.raises(OSError) as raised,
    ):
        publish_durable(source, destination)

    assert raised.value.errno == errno.ENOSPC
    assert source.exists()
    assert not destination.exists()
