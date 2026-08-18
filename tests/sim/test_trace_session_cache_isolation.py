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

import os
from pathlib import Path
from unittest.mock import patch

from tests.conftest import MINIMAL_FST_BYTES

from booley.sim.trace_session import TraceSession


def _session(work_dir: Path, cache_root: Path) -> TraceSession:
    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
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

    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
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

    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
        # Ticket B ran first and left a store in its cache bucket.
        (TraceSession(b).cache_dir / "trace.fst").write_bytes(MINIMAL_FST_BYTES)
        # Ticket A has produced nothing at all.
        assert TraceSession(a).find() is None


def test_explicit_cache_key_still_wins(tmp_path):
    """Content-addressed keys are untouched by the path-digest fallback."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()
    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
        assert TraceSession(work, cache_key="deadbeef").cache_dir.name == "deadbeef"


def test_setup_bwave_paths_defaults_to_the_session_bucket(tmp_path):
    """Writer and checker must derive the same store path (F-24).

    ``setup_bwave_paths`` used to rebuild the bucket as
    ``_bwave_cache_root() / work_dir.name``, i.e. the pre-digest name, so the
    FIFO streamer wrote ``/tmp/bwave/sim/trace.fst`` while every reader probed
    ``/tmp/bwave/sim-<digest>/trace.fst`` and reported it missing.
    """
    from booley.sim.bwave_fifo import setup_bwave_paths

    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()

    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
        _fifo, bwave_path, _proc, _use_fifo, _fd = setup_bwave_paths(work, False, None)
        assert bwave_path == TraceSession(work).bwave_path
    assert bwave_path.parent != cache_root / "sim", "bare work_dir.name bucket is the F-24 bug"


def test_start_fifo_streams_into_the_session_store_path(tmp_path):
    """TraceSession.start_fifo hands the streamer its own ``bwave_path``."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()
    seen: list[Path] = []

    def _fake_start(work_dir, bwave_path, trace_scope):
        seen.append(bwave_path)
        return None, True, None

    with (
        patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root),
        patch("booley.sim.bwave_fifo.can_stream_bwave_fifo", return_value=True),
        patch("booley.sim.bwave_fifo.start_bwave_fifo", side_effect=_fake_start),
    ):
        session = TraceSession(work)
        session.start_fifo()
        assert seen == [session.bwave_path]


def test_fresher_published_store_beats_stale_cache(tmp_path):
    """A re-sim publishes beside the artifacts; the old cached store loses."""
    cache_root = tmp_path / "bwave"
    work = tmp_path / "sim"
    work.mkdir()

    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
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

    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
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

    with patch("booley.sim.trace_session._bwave_cache_root", return_value=cache_root):
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
