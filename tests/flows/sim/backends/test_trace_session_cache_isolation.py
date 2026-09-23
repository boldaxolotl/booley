"""Trace-cache isolation — one bucket per sim directory.

Field failure (benchmark batches 1-2): a run invoked
``bwave register <ticket>/sim-trace/.../sim`` and got a waveform belonging to
a completely different design — signals from an unrelated serial-comm
testbench answered questions about an AXI bridge. Root cause: the tmpdir
cache bucket was keyed on ``work_dir.name``, and every ticket's sim output
directory is called ``sim``, so all of them shared ``/tmp/bwave/sim`` inside
one container. ``find()`` checks that cache before the work dir, so whichever
ticket wrote there last answered for everyone, silently.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

from tests.conftest import MINIMAL_FST_BYTES

from booley.flows.sim.trace_session import TraceSession


def _cache_patch(cache_root: Path):
    def cache_dir(work_dir: Path, cache_key: str | None = None) -> Path:
        resolved = str(work_dir.resolve())
        digest = hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:12]
        path = cache_root / (cache_key or f"{work_dir.name}-{digest}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    return patch("booley.flows.sim.trace_session.waveform_cache_dir", side_effect=cache_dir)


def _session(work_dir: Path, cache_root: Path) -> TraceSession:
    with _cache_patch(cache_root):
        session = TraceSession(work_dir)
        # Touch cache_dir inside the patch so the bucket is created there.
        _ = session.cache_dir
        return session


def test_same_named_work_dirs_get_distinct_cache_buckets(tmp_path):
    cache_root = tmp_path / "bwave"
    a = tmp_path / "ticket_a" / "sim"
    b = tmp_path / "ticket_b" / "sim"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    with _cache_patch(cache_root):
        bucket_a = TraceSession(a).cache_dir
        bucket_b = TraceSession(b).cache_dir

    assert bucket_a != bucket_b, "same-named sim dirs must not share a cache bucket"
    assert bucket_a.name.startswith("sim-"), "readable prefix is kept"
    assert bucket_b.name.startswith("sim-")


def test_other_tickets_trace_is_not_served(tmp_path):
    """The exact cross-design bind: B's cached trace must not answer for A."""
    cache_root = tmp_path / "bwave"
    a = tmp_path / "ticket_a" / "sim"
    b = tmp_path / "ticket_b" / "sim"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    with _cache_patch(cache_root):
        # Ticket B ran first and left a store in its cache bucket.
        (TraceSession(b).cache_dir / "trace.fst").write_bytes(MINIMAL_FST_BYTES)
        # Ticket A has produced nothing at all.
        assert TraceSession(a).find() is None


def test_explicit_cache_key_still_wins(tmp_path):
    """Content-addressed keys are untouched by the path-digest fallback."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()
    with _cache_patch(cache_root):
        assert TraceSession(work, cache_key="deadbeef").cache_dir.name == "deadbeef"


def test_start_fifo_streams_into_the_session_store_path(tmp_path):
    """TraceSession.start_fifo hands the streamer its own ``bwave_path``."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()
    seen: list[Path] = []

    def _fake_start(_fifo_path, store_path, **_kwargs):
        seen.append(store_path)
        return object()

    with (
        _cache_patch(cache_root),
        patch(
            "booley.flows.sim.trace_session.start_streaming_conversion",
            side_effect=_fake_start,
        ),
    ):
        session = TraceSession(work)
        session.start_fifo()
        assert seen == [session.bwave_path]


def test_fresher_published_store_beats_stale_cache(tmp_path):
    """A re-sim publishes beside the artifacts; the old cached store loses."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()

    with _cache_patch(cache_root):
        session = TraceSession(work)
        cached = session.cache_dir / "trace.fst"
        cached.write_bytes(MINIMAL_FST_BYTES)
        os.utime(cached, (1_000_000, 1_000_000))

        published = work / "trace.fst"
        published.write_bytes(MINIMAL_FST_BYTES)
        os.utime(published, (2_000_000, 2_000_000))

        assert session.find() == published


def test_equal_mtime_prefers_host_visible_published_store(tmp_path):
    """copy2 preserves mtime; reports must name the project copy on that tie."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()

    with _cache_patch(cache_root):
        session = TraceSession(work)
        cached = session.cache_dir / "trace.fst"
        cached.write_bytes(MINIMAL_FST_BYTES)
        published = work / "trace.fst"
        published.write_bytes(MINIMAL_FST_BYTES)
        os.utime(cached, (2_000_000, 2_000_000))
        os.utime(published, (2_000_000, 2_000_000))

        assert session.find() == published


def test_reset_for_run_removes_every_candidate_from_an_earlier_attempt(tmp_path):
    """A failed trace attempt must not earn TRACE_OK from surviving artifacts."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    run_dir = tmp_path / "run"
    work.mkdir()
    run_dir.mkdir()

    with _cache_patch(cache_root):
        session = TraceSession(work)
        old_paths = (
            session.bwave_path,
            session.work_bwave_path,
            work / "trace.vcd",
            run_dir / "dump.vcd",
        )
        for path in old_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(MINIMAL_FST_BYTES)

        session.reset_for_run((run_dir / "dump.vcd",))

        assert all(not path.exists() for path in old_paths)
        assert session.find() is None


def test_successful_postprocess_consumes_raw_vcd_outside_work_dir(tmp_path):
    cache_root = tmp_path / "bwave"
    work = tmp_path / "runtime" / "sim"
    run_dir = tmp_path / "project"
    work.mkdir(parents=True)
    run_dir.mkdir()
    raw_vcd = run_dir / "dump.vcd"
    raw_vcd.write_text("$date\n$end\n", encoding="utf-8")

    def _build(_vcd_path: Path, bwave_path: Path, **_kwargs):
        from booley.bwave.waveform_store import ConversionResult, inspect_store

        bwave_path.write_bytes(MINIMAL_FST_BYTES)
        return ConversionResult(
            bwave_path,
            inspection=inspect_store(bwave_path, probe_reader=False),
        )

    with (
        _cache_patch(cache_root),
        patch(
            "booley.flows.sim.trace_session.convert_vcd",
            side_effect=_build,
        ),
    ):
        session = TraceSession(work, trace_scope="tb")
        session.postprocess(raw_vcd)

        assert not raw_vcd.exists()
        assert not (work / "trace.vcd").exists()
        assert session.work_bwave_path.is_file()


def test_failed_postprocess_moves_raw_vcd_and_diagnostic_into_work_dir(tmp_path):
    cache_root = tmp_path / "bwave"
    work = tmp_path / "runtime" / "sim"
    run_dir = tmp_path / "project"
    work.mkdir(parents=True)
    run_dir.mkdir()
    raw_vcd = run_dir / "dump.vcd"
    raw_vcd.write_text("$date\n$end\n", encoding="utf-8")
    leaked_diagnostic = run_dir / "trace.fst.stderr"

    def _fail(_vcd_path: Path, bwave_path: Path, **_kwargs):
        from booley.bwave.waveform_store import ConversionResult

        return ConversionResult(
            bwave_path,
            failure_kind="conversion_failed",
            detail="conversion failed",
        )

    with (
        _cache_patch(cache_root),
        patch(
            "booley.flows.sim.trace_session.convert_vcd",
            side_effect=_fail,
        ),
    ):
        session = TraceSession(work, trace_scope="tb")
        session.postprocess(raw_vcd)

        assert not raw_vcd.exists()
        assert (work / "trace.vcd").is_file()
        assert not leaked_diagnostic.exists()
        assert session.persisted_stderr_path.read_text(encoding="utf-8") == ("conversion failed\n")
