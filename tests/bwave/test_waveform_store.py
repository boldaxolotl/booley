"""Behavioral contract for B-Wave-owned waveform-store mechanics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from time import monotonic

import pytest

from booley.bwave import cli
from booley.bwave import waveform_store as stores
from tests.bwave.native_binary import fake_native_binary
from tests.bwave.session_store_support import isolate_temporary_store
from tests.conftest import FST_HEADER_BYTES, MINIMAL_FST_BYTES
from tests.path_tool_fake import PathToolFake
from tests.time_budget import hang_guard_s

_VCD = "$date now $end\n$timescale 1ns $end\n$scope module tb $end\n"


@pytest.fixture
def fake_native(monkeypatch, tmp_path) -> PathToolFake:
    return fake_native_binary(tmp_path / "native", monkeypatch)


def _fail_copy(*_args: object, **_kwargs: object) -> None:
    raise OSError("copy failed")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, stores.StoreStructure.MISSING),
        (b"", stores.StoreStructure.TOO_SMALL),
        (b"not-an-fst" * 4, stores.StoreStructure.MALFORMED),
        (FST_HEADER_BYTES, stores.StoreStructure.NO_VALUE_CHANGES),
        (MINIMAL_FST_BYTES, stores.StoreStructure.VIABLE),
        # A block longer than the file, and a block of zero length.
        (
            FST_HEADER_BYTES + bytes([9]) + (999).to_bytes(8, "big"),
            stores.StoreStructure.MALFORMED,
        ),
        (FST_HEADER_BYTES + bytes([9]) + (1).to_bytes(8, "big"), stores.StoreStructure.MALFORMED),
        # More non-value-change blocks than the scan reads.
        (
            FST_HEADER_BYTES + (bytes([9]) + (8).to_bytes(8, "big")) * 10_000,
            stores.StoreStructure.MALFORMED,
        ),
    ],
    ids=["missing", "empty", "junk", "header", "viable", "overrun", "zero-block", "scan-limit"],
)
def test_structural_classification(tmp_path: Path, payload: bytes | None, expected) -> None:
    path = tmp_path / "trace.fst"
    if payload is not None:
        path.write_bytes(payload)

    inspection = stores.inspect_store(path, probe_reader=False)

    assert inspection.structure is expected
    assert inspection.structurally_usable is (expected is stores.StoreStructure.VIABLE)


@pytest.mark.parametrize(
    ("signals", "expected_scope", "failure_kind"),
    [(2, "tb.dut", ""), (2, "other", "scope_mismatch"), (0, None, "zero_signals")],
    ids=["queryable", "scope-mismatch", "zero-signals"],
)
def test_reader_probe_classifies_a_viable_store(
    tmp_path: Path, fake_native, signals: int, expected_scope: str | None, failure_kind: str
) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    data = {
        "scope_prefix": "tb.dut",
        "root_scopes": ["tb"],
        "signal_count": signals,
        "total_ticks": 10,
    }
    fake_native.answer("list", stdout=json.dumps({"data": data}))

    inspection = stores.inspect_store(path, expected_scope=expected_scope)

    assert inspection.structurally_usable is True
    assert inspection.failure_kind == failure_kind
    assert inspection.queryable is (not failure_kind)


def test_scoped_conversion_removes_partial_output_before_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    destination = tmp_path / "trace.fst"
    calls: list[list[str]] = []
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    def run(command, **_kwargs):
        calls.append(list(command))
        if "--scope" in command:
            destination.write_bytes(b"partial")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"no match")
        assert not destination.exists(), "invalid scoped output survived into fallback"
        destination.write_bytes(MINIMAL_FST_BYTES)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(stores.subprocess, "run", run)

    result = stores.convert_vcd(
        vcd,
        destination,
        scope="tb.missing",
        allow_unscoped_fallback=True,
    )

    assert result.success is True
    assert len(result.attempts) == 2
    assert "--scope" in calls[0]
    assert "--scope" not in calls[1]
    assert result.attempts[0].stderr == "no match"


def test_conversion_deadline_bounds_every_build_attempt(tmp_path: Path, monkeypatch) -> None:
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    destination = tmp_path / "trace.fst"
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")
    clock = {"now": 1000.0}
    monkeypatch.setattr(stores, "monotonic", lambda: clock["now"])
    timeouts: list[float] = []

    def run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock["now"] += 100  # the scoped build eats the whole budget, then fails
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(stores.subprocess, "run", run)

    result = stores.convert_vcd(
        vcd,
        destination,
        scope="tb",
        allow_unscoped_fallback=True,
        deadline=1100.0,
    )

    assert timeouts == [100.0]  # the remaining budget, not the fixed 900 s
    assert result.success is False
    assert result.failure_kind == "conversion_failed"
    assert len(result.attempts) == 1  # the unscoped retry is skipped
    assert result.detail.startswith("conversion budget exhausted")

    result = stores.convert_vcd(vcd, destination, scope="tb", deadline=1100.0)

    assert timeouts == [100.0]  # an already-spent budget starts no build at all
    assert result.attempts == ()
    assert result.detail.startswith("conversion budget exhausted")


def test_refuse_policy_never_overwrites_even_an_invalid_destination(
    tmp_path: Path, monkeypatch
) -> None:
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    destination = tmp_path / "trace.fst"
    destination.write_bytes(b"partial")
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    result = stores.convert_vcd(
        vcd,
        destination,
        existing_store=stores.ExistingStorePolicy.REFUSE,
    )

    assert result.success is False
    assert result.failure_kind == "existing_store"
    assert destination.read_bytes() == b"partial"


def test_discovery_preserves_simulation_manifest_byte_for_byte(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    manifest = tmp_path / "trace_status.json"
    original = b'{"attempt":"real-simulation"}\n'
    manifest.write_bytes(original)
    (tmp_path / "trace.fst").write_bytes(MINIMAL_FST_BYTES)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.selected == tmp_path / "trace.fst"
    assert manifest.read_bytes() == original


def test_invalid_newer_cache_does_not_short_circuit_vcd_conversion(
    tmp_path: Path, fake_native
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    invalid = cache / "trace.fst"
    invalid.write_bytes(b"newer but invalid")
    vcd = tmp_path / "trace.vcd"
    vcd.write_text(_VCD, encoding="utf-8")
    os.utime(invalid, (2_000_000, 2_000_000))
    os.utime(vcd, (1_000_000, 1_000_000))
    fake_native.answer("build", output=MINIMAL_FST_BYTES)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.conversion is not None
    assert result.conversion.success is True
    assert result.selected == invalid
    assert invalid.read_bytes() == MINIMAL_FST_BYTES
    # Converted into the cache only; nothing is published into the work directory.
    assert not (tmp_path / "trace.fst").exists()


@pytest.mark.parametrize(
    ("files", "selected", "failure_kind"),
    [
        ({}, None, ""),
        ({"a.fst": MINIMAL_FST_BYTES, "b.fst": MINIMAL_FST_BYTES}, None, "ambiguous"),
        ({"trace.vcd": _VCD.encode()}, "trace.vcd", ""),
        # Some simulators leave the dump in a regular file named like the pipe.
        ({"trace.fifo": _VCD.encode()}, "trace.vcd", ""),
        ({"trace.fifo": b"not a waveform"}, None, ""),
        ({"trace.fifo": b""}, None, ""),
    ],
    ids=["no-artifact", "two-stores", "raw-vcd", "fifo-dump", "fifo-not-vcd", "fifo-empty"],
)
def test_discovery_selects(
    tmp_path: Path, files: dict[str, bytes], selected: str | None, failure_kind: str
) -> None:
    for name, payload in files.items():
        (tmp_path / name).write_bytes(payload)

    result = stores.discover_waveform(
        tmp_path, cache_dir=tmp_path / "cache", convert_raw_vcd=False
    )

    assert result.selected == (None if selected is None else tmp_path / selected)
    assert result.failure_kind == failure_kind
    assert ("Multiple *.fst files" in result.detail) is (failure_kind == "ambiguous")
    assert result.conversion is None
    if selected is not None:
        assert (tmp_path / selected).read_bytes() == _VCD.encode()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_discovery_does_not_open_a_real_fifo(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "trace.fifo")

    assert stores.discover_waveform(tmp_path, cache_dir=tmp_path / "cache").selected is None


def test_discovery_falls_back_to_the_fifo_dump_it_cannot_copy(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "trace.fifo").write_text(_VCD, encoding="utf-8")
    monkeypatch.setattr(shutil, "copy2", _fail_copy)

    result = stores.discover_waveform(
        tmp_path, cache_dir=tmp_path / "cache", convert_raw_vcd=False
    )

    assert result.selected == tmp_path / "trace.fifo"


def test_materialize_keeps_a_newer_copy_of_the_fifo_dump(tmp_path: Path) -> None:
    fifo, vcd = tmp_path / "trace.fifo", tmp_path / "trace.vcd"
    fifo.write_text(_VCD, encoding="utf-8")
    vcd.write_text("newer copy", encoding="utf-8")
    os.utime(fifo, (1_000_000, 1_000_000))

    # Private: discovery calls this only while trace.vcd is absent, so the branch runs only
    # when the file appears in between.
    assert stores._materialize_fifo_vcd(fifo, vcd) == vcd
    assert vcd.read_text(encoding="utf-8") == "newer copy"


def test_discovery_reports_directory_errors(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    original_glob = Path.glob

    def failing_glob(directory, pattern):
        if directory == cache:
            raise OSError("permission denied")
        return original_glob(directory, pattern)

    monkeypatch.setattr(Path, "glob", failing_glob)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.failure_kind == "ambiguous"
    assert "Unable to inspect waveform directory" in result.detail


def test_conversion_reports_missing_input_and_binary(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "trace.fst"

    missing = stores.convert_vcd(tmp_path / "missing.vcd", destination)
    assert missing.failure_kind == "missing_input"

    source = tmp_path / "trace.vcd"
    source.write_text("$scope module tb $end\n", encoding="utf-8")
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: None)
    unavailable = stores.convert_vcd(source, destination)
    assert unavailable.failure_kind == "missing_binary"


def test_conversion_failure_preserves_attempt_diagnostics(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "trace.vcd"
    source.write_text("$scope module tb $end\n", encoding="utf-8")
    destination = tmp_path / "trace.fst"
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 7, stdout=b"", stderr=b"conversion failed"
        ),
    )

    result = stores.convert_vcd(source, destination)

    assert result.success is False
    assert result.failure_kind == "conversion_failed"
    assert result.attempts[-1].return_code == 7
    assert "conversion failed" in result.detail
    assert stores.conversion_failure_messages(result, warning="failed")


def test_conversion_result_and_discovery_success_properties() -> None:
    assert stores.ConversionResult(Path("trace.fst")).success is False
    assert stores.DiscoveryResult(None).success is False


def test_conversion_warnings_are_the_warning_lines_of_a_successful_build(tmp_path: Path) -> None:
    store = tmp_path / "trace.fst"
    store.write_bytes(MINIMAL_FST_BYTES)
    stderr = "WARNING: cut\n# clock regularity: ok\nWARNING: odd\n# wrote trace.fst\n"
    attempt = stores.ConversionAttempt(None, 0, stderr)
    done = stores.ConversionResult(
        store, inspection=stores.inspect_store(store, probe_reader=False), attempts=(attempt,)
    )
    assert done.warnings == ("WARNING: cut", "WARNING: odd")
    failed = stores.ConversionResult(store, failure_kind="conversion_failed", attempts=(attempt,))
    assert failed.warnings == ()
    assert stores.ConversionResult(store).warnings == ()


def test_existing_valid_store_is_reused(tmp_path: Path) -> None:
    source = tmp_path / "trace.vcd"
    source.write_text("raw", encoding="utf-8")
    destination = tmp_path / "trace.fst"
    destination.write_bytes(MINIMAL_FST_BYTES)

    result = stores.convert_vcd(
        source,
        destination,
        existing_store=stores.ExistingStorePolicy.REUSE_STRUCTURALLY_VALID,
    )

    assert result.success is True
    assert result.events == ("reused existing store",)


def test_structural_scan_handles_nonregular_and_stat_errors(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "trace.fst"
    directory.mkdir()
    assert (
        stores.inspect_store(directory, probe_reader=False).structure
        is stores.StoreStructure.NOT_REGULAR
    )

    path = tmp_path / "unreadable.fst"
    original_stat = Path.stat

    def fail_stat(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("permission denied")
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_stat)
    assert (
        stores.inspect_store(path, probe_reader=False).structure
        is stores.StoreStructure.NOT_REGULAR
    )


@pytest.mark.parametrize(
    ("failure", "expected_kind"),
    [
        ("timeout", "reader_timeout"),
        ("oserror", "reader_error"),
        ("returncode", "reader_error"),
        ("metadata", "malformed_metadata"),
    ],
)
def test_metadata_probe_failures_are_classified(
    tmp_path: Path, monkeypatch, failure: str, expected_kind: str
) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    def run(*_args, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("bwave", 60)
        if failure == "oserror":
            raise OSError("exec failed")
        if failure == "returncode":
            return subprocess.CompletedProcess([], 2, stdout="", stderr="bad trace")
        return subprocess.CompletedProcess([], 0, stdout="not json", stderr="")

    monkeypatch.setattr(stores.subprocess, "run", run)
    inspection = stores.inspect_store(path)

    assert inspection.queryable is False
    assert inspection.failure_kind == expected_kind


def test_discovery_reports_failed_conversion(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("raw", encoding="utf-8")
    failure = stores.ConversionResult(
        cache / "trace.fst", failure_kind="conversion_failed", detail="bad"
    )
    monkeypatch.setattr(stores, "convert_vcd", lambda *_args, **_kwargs: failure)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.selected == vcd
    assert result.conversion is failure


def test_metadata_probe_reports_missing_binary(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: None)
    inspection = stores.inspect_store(path)
    assert inspection.failure_kind == "missing_binary"


@pytest.mark.skipif(os.name == "nt", reason="killing the .cmd launcher leaves its child running")
def test_build_timeout_returns_bounded_completed_process(tmp_path: Path, fake_native) -> None:
    source = tmp_path / "trace.vcd"
    source.write_text(_VCD, encoding="utf-8")
    fake_native.answer("build", hang_s=hang_guard_s())

    result = stores.convert_vcd(source, tmp_path / "trace.fst", deadline=monotonic() + 2)

    assert result.attempts[-1].return_code == 124
    assert "timed out after" in result.attempts[-1].stderr


@pytest.mark.skipif(os.name == "nt", reason="FIFO requires POSIX")
def test_streaming_conversion_fake_process_covers_lifecycle(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        pid = 123

        def __init__(self, return_code: int) -> None:
            self.return_code = return_code
            self.killed = False

        def poll(self):
            return self.return_code

        def wait(self, timeout=None):
            return self.return_code

        def kill(self):
            self.killed = True

    process = FakeProcess(0)

    def popen(command, **_kwargs):
        Path(command[command.index("-o") + 1]).write_bytes(MINIMAL_FST_BYTES)
        return process

    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")
    monkeypatch.setattr(stores.subprocess, "Popen", popen)
    fifo = tmp_path / "trace.fifo"
    store = tmp_path / "trace.fst"
    stderr = tmp_path / "trace.stderr"
    conversion = stores.start_streaming_conversion(fifo, store, stderr_path=stderr, scope="tb")
    assert conversion is not None
    (store.with_name("trace.fst.progress")).write_bytes(b"progress")
    stderr.write_text("native warning", encoding="utf-8")
    assert conversion.progress_size() > 0
    assert conversion.stderr_tail() == "native warning"
    conversion.kill()
    result = conversion.finish()

    assert result.success is True
    assert "wrote trace.fst" in result.events[0]
    assert not fifo.exists()


def _descriptors_on(identity: tuple[int, int]) -> list[int]:
    """Descriptors of this process that refer to the file with *identity* (device, inode)."""
    found: list[int] = []
    for entry in Path("/dev/fd").iterdir():
        try:
            status = os.fstat(int(entry.name))
        except OSError:
            continue
        if (status.st_dev, status.st_ino) == identity:
            found.append(int(entry.name))
    return found


@pytest.mark.skipif(os.name == "nt", reason="FIFO requires POSIX")
def test_backlog_counts_unread_bytes_through_a_read_only_descriptor(
    tmp_path: Path, fake_bwave, monkeypatch
) -> None:
    """macOS answers FIONREAD on a read-write FIFO descriptor with 0: ask a read-only one."""
    import fcntl

    monkeypatch.setenv("FAKE_BWAVE_MODE", "never_read")
    fifo = tmp_path / "pipe.vcd"
    conversion = stores.start_streaming_conversion(
        fifo, tmp_path / "s.fst", stderr_path=tmp_path / "e"
    )
    assert conversion is not None
    modes: list[int] = []
    real_ioctl = fcntl.ioctl

    def spy(fd: int, *args):
        modes.append(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE)
        return real_ioctl(fd, *args)

    monkeypatch.setattr(fcntl, "ioctl", spy)
    try:
        writer = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
        try:
            assert os.write(writer, b"x" * 100) == 100
        finally:
            os.close(writer)
        assert conversion.backlog() == 100
        assert modes == [os.O_RDONLY]
    finally:
        conversion.kill()
        conversion.finish(timeout_s=hang_guard_s())
    assert _descriptors_on(conversion.fifo_identity) == []


@pytest.mark.skipif(os.name == "nt", reason="FIFO requires POSIX")
def test_streaming_start_unavailable_and_popen_failure(tmp_path: Path, monkeypatch) -> None:
    fifo = tmp_path / "trace.fifo"
    store = tmp_path / "trace.fst"
    stderr = tmp_path / "trace.stderr"
    monkeypatch.setattr(stores.os, "name", "nt")
    assert stores.start_streaming_conversion(fifo, store, stderr_path=stderr) is None
    monkeypatch.setattr(stores.os, "name", "posix")
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: None)
    assert stores.start_streaming_conversion(fifo, store, stderr_path=stderr) is None
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")
    seen: list[tuple[int, int]] = []

    def failing_popen(*_args, stdin: int, **_kwargs):
        status = os.fstat(stdin)
        seen.append((status.st_dev, status.st_ino))
        raise OSError("spawn failed")

    monkeypatch.setattr(stores.subprocess, "Popen", failing_popen)
    assert stores.start_streaming_conversion(fifo, store, stderr_path=stderr) is None
    assert not fifo.exists()
    assert _descriptors_on(seen[0]) == []


@pytest.mark.skipif(os.name == "nt", reason="FIFO requires POSIX")
def test_strict_streaming_start_never_touches_an_existing_path(tmp_path: Path, fake_bwave) -> None:
    """``replace_existing=False``: a user's file stays and no directory appears."""
    existing = tmp_path / "user.vcd"
    existing.write_text("user data", encoding="utf-8")
    strict = {"stderr_path": tmp_path / "e", "replace_existing": False}

    assert stores.start_streaming_conversion(existing, tmp_path / "s.fst", **strict) is None
    assert (
        stores.start_streaming_conversion(tmp_path / "no" / "x.vcd", tmp_path / "s", **strict)
        is None
    )

    assert existing.read_text(encoding="utf-8") == "user data"
    assert not (tmp_path / "no").exists()


@pytest.mark.skipif(os.name == "nt", reason="FIFO requires POSIX")
def test_close_input_keeps_a_path_the_writer_replaced(tmp_path: Path, fake_bwave) -> None:
    """Only the pipe this conversion created is removed; a replacement file is the user's."""
    fifo = tmp_path / "pipe.vcd"
    conversion = stores.start_streaming_conversion(
        fifo, tmp_path / "s.fst", stderr_path=tmp_path / "e"
    )
    assert conversion is not None
    fifo.unlink()
    fifo.write_text("replacement", encoding="utf-8")

    result = conversion.finish(timeout_s=hang_guard_s())

    assert fifo.read_text(encoding="utf-8") == "replacement"
    assert result.success is False


def test_streaming_failure_does_not_report_success(tmp_path: Path, monkeypatch) -> None:
    class FailedProcess:
        pid = 456

        def poll(self):
            return 3

        def wait(self, timeout=None):
            return 3

        def kill(self):
            return None

    monkeypatch.setattr(
        stores,
        "inspect_store",
        lambda *_args, **_kwargs: stores.StoreInspection(
            tmp_path / "trace.fst", stores.StoreStructure.VIABLE
        ),
    )
    conversion = stores.StreamingConversion(
        FailedProcess(),  # type: ignore[arg-type]
        -1,
        -1,
        tmp_path / "trace.fifo",
        tmp_path / "trace.fst",
        tmp_path / "trace.stderr",
        None,
        (0, 0),
    )
    monkeypatch.setattr(stores.os, "close", lambda _fd: None)
    result = conversion.finish()

    assert result.success is False
    assert "rc=3" in result.events[-1]


@pytest.mark.parametrize("outcome", ["published", "copy-fails", "build-fails"])
def test_cli_find_trace_converts_and_publishes(
    tmp_path: Path, monkeypatch, capsys, fake_native, outcome: str
) -> None:
    isolate_temporary_store(monkeypatch, tmp_path)  # the conversion cache lives there
    work = tmp_path / "work"
    work.mkdir()
    (work / "trace.vcd").write_text(_VCD, encoding="utf-8")
    if outcome == "build-fails":
        fake_native.answer("build", returncode=1, stderr="bad conversion")
    else:
        fake_native.answer("build", output=MINIMAL_FST_BYTES)
    if outcome == "copy-fails":
        monkeypatch.setattr(shutil, "copy2", _fail_copy)

    found = cli.find_trace(work)

    expected = {
        "published": work / "trace.fst",
        "copy-fails": stores.waveform_cache_dir(work) / "trace.fst",
        "build-fails": work / "trace.vcd",
    }[outcome]
    assert found == expected
    if outcome == "build-fails":
        assert "conversion failed" in capsys.readouterr().err
    else:
        assert found.read_bytes() == MINIMAL_FST_BYTES


def test_cli_find_trace_raises_for_ambiguous_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "discover_waveform",
        lambda *_args, **_kwargs: stores.DiscoveryResult(
            None, failure_kind="ambiguous", detail="ambiguous"
        ),
    )
    monkeypatch.setattr(cli, "waveform_cache_dir", lambda _work_dir: tmp_path / "cache")
    with pytest.raises(SystemExit, match="ambiguous"):
        cli.find_trace(tmp_path)


def test_trace_session_find_and_cleanup_publish_conversion(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from booley.flows.sim import trace_session

    cache = tmp_path / "cache"
    cache.mkdir()
    selected = cache / "trace.fst"
    selected.write_bytes(MINIMAL_FST_BYTES)
    conversion = stores.ConversionResult(
        selected,
        inspection=stores.StoreInspection(selected, stores.StoreStructure.VIABLE),
        events=("converted",),
        detail="native note",
    )
    monkeypatch.setattr(
        trace_session,
        "discover_waveform",
        lambda *_args, **_kwargs: stores.DiscoveryResult(selected, conversion=conversion),
    )
    monkeypatch.setattr(trace_session, "waveform_cache_dir", lambda *_args, **_kwargs: cache)
    session = trace_session.TraceSession(tmp_path / "work")
    assert session.find() == session.work_bwave_path
    assert session.work_bwave_path.exists()

    class Finished:
        def finish(self):
            return conversion

    session.cleanup_fifo(Finished())
    output = capsys.readouterr().out
    assert "converted" in output
    assert "native note" in output

    monkeypatch.setattr(
        trace_session, "discover_waveform", lambda *_args, **_kwargs: stores.DiscoveryResult(None)
    )
    assert session.inspect(None).usable is False


def test_trace_session_find_raises_for_ambiguous_discovery(tmp_path: Path, monkeypatch) -> None:
    from booley.flows.sim import trace_session

    monkeypatch.setattr(
        trace_session,
        "discover_waveform",
        lambda *_args, **_kwargs: stores.DiscoveryResult(
            None, failure_kind="ambiguous", detail="ambiguous"
        ),
    )
    with pytest.raises(SystemExit, match="ambiguous"):
        trace_session.TraceSession(tmp_path).find()
