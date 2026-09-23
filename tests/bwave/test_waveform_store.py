"""Behavioral contract for B-Wave-owned waveform-store mechanics."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from booley.bwave import waveform_store as stores
from tests.conftest import FST_HEADER_BYTES, MINIMAL_FST_BYTES


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", stores.StoreStructure.TOO_SMALL),
        (b"not-an-fst" * 4, stores.StoreStructure.MALFORMED),
        (FST_HEADER_BYTES, stores.StoreStructure.NO_VALUE_CHANGES),
        (MINIMAL_FST_BYTES, stores.StoreStructure.VIABLE),
    ],
)
def test_structural_classification(tmp_path: Path, payload: bytes, expected) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(payload)

    assert stores.inspect_store(path, probe_reader=False).structure is expected


def test_missing_store_is_distinct(tmp_path: Path) -> None:
    inspection = stores.inspect_store(tmp_path / "missing.fst", probe_reader=False)

    assert inspection.structure is stores.StoreStructure.MISSING
    assert inspection.structurally_usable is False


def test_structural_viability_does_not_imply_queryability(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "/bin/bwave")
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, stdout="", stderr="bad"),
    )

    inspection = stores.inspect_store(path)

    assert inspection.structurally_usable is True
    assert inspection.queryable is False
    assert inspection.failure_kind == "reader_error"


def test_reader_metadata_and_expected_scope(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "/bin/bwave")
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "data": {
                        "scope_prefix": "tb.dut",
                        "root_scopes": ["tb"],
                        "signal_count": 2,
                        "total_ticks": 10,
                        "signals": [{"name": "clk"}],
                    }
                }
            ),
            stderr="",
        ),
    )

    assert stores.inspect_store(path, expected_scope="tb.dut").queryable is True
    mismatch = stores.inspect_store(path, expected_scope="other")
    assert mismatch.failure_kind == "scope_mismatch"


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
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    invalid = cache / "trace.fst"
    invalid.write_bytes(b"newer but invalid")
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    os.utime(invalid, (2_000_000, 2_000_000))
    os.utime(vcd, (1_000_000, 1_000_000))
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    def run(command, **_kwargs):
        invalid.write_bytes(MINIMAL_FST_BYTES)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(stores.subprocess, "run", run)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.conversion is not None
    assert result.conversion.success is True
    assert result.selected == cache / "trace.fst"
    assert result.selected.read_bytes() == MINIMAL_FST_BYTES


def test_discovery_returns_cache_store_without_publishing_to_work_dir(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    def run(command, **_kwargs):
        destination = cache / "trace.fst"
        destination.write_bytes(MINIMAL_FST_BYTES)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(stores.subprocess, "run", run)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.selected == cache / "trace.fst"
    assert not (tmp_path / "trace.fst").exists()


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


def test_existing_valid_store_is_reused_and_cache_key_is_stable(tmp_path: Path) -> None:
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
    assert stores.waveform_cache_dir(tmp_path, "fixed-key").name == "fixed-key"


def test_zero_signal_store_is_structural_but_not_queryable(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "data": {
                        "scope_prefix": "tb",
                        "root_scopes": [],
                        "signal_count": 0,
                        "total_ticks": 0,
                    }
                }
            ),
            stderr="",
        ),
    )

    inspection = stores.inspect_store(path)

    assert inspection.structurally_usable is True
    assert inspection.queryable is False
    assert inspection.failure_kind == "zero_signals"


@pytest.mark.parametrize(
    "payload",
    [
        FST_HEADER_BYTES + bytes([9]) + (999).to_bytes(8, "big"),
        FST_HEADER_BYTES + bytes([9]) + (1).to_bytes(8, "big"),
    ],
)
def test_structural_scan_rejects_bad_block_chains(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(payload)

    assert (
        stores.inspect_store(path, probe_reader=False).structure is stores.StoreStructure.MALFORMED
    )


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


def test_structural_scan_limit_is_reported_as_malformed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(FST_HEADER_BYTES + bytes([9]) + (1).to_bytes(8, "big") + b"x")
    monkeypatch.setattr(stores, "_FST_MAX_BLOCK_SCAN", 1)

    assert (
        stores.inspect_store(path, probe_reader=False).structure is stores.StoreStructure.MALFORMED
    )


@pytest.mark.parametrize("failure", ["timeout", "oserror", "returncode", "metadata"])
def test_metadata_probe_failures_are_classified(tmp_path: Path, monkeypatch, failure: str) -> None:
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
    assert inspection.failure_kind in {"reader_timeout", "reader_error", "malformed_metadata"}


def test_discovery_without_conversion_returns_raw_vcd(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")

    result = stores.discover_waveform(tmp_path, cache_dir=cache, convert_raw_vcd=False)

    assert result.selected == vcd
    assert result.conversion is None


def test_discovery_without_any_artifact_returns_empty_result(tmp_path: Path) -> None:
    result = stores.discover_waveform(tmp_path, cache_dir=tmp_path / "cache")
    assert result.selected is None


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


def test_stores_in_reports_multiple_valid_stores(tmp_path: Path) -> None:
    first = tmp_path / "a.fst"
    second = tmp_path / "b.fst"
    first.write_bytes(MINIMAL_FST_BYTES)
    second.write_bytes(MINIMAL_FST_BYTES)

    valid, invalid, detail = stores._stores_in(tmp_path)

    assert valid == [first, second]
    assert invalid == ()
    assert "Multiple *.fst files" in detail


def test_stores_in_ignores_missing_directory(tmp_path: Path) -> None:
    assert stores._stores_in(tmp_path / "missing") == ([], (), "")


def test_fifo_vcd_materialization_handles_cache_and_copy_failure(
    tmp_path: Path, monkeypatch
) -> None:
    fifo = tmp_path / "trace.fifo"
    vcd = tmp_path / "trace.vcd"
    fifo.write_text(
        "$date now $end\n$timescale 1ns $end\n$scope module tb $end\n", encoding="utf-8"
    )

    assert stores._materialize_fifo_vcd(fifo, vcd) == vcd
    assert stores._materialize_fifo_vcd(fifo, vcd) == vcd
    monkeypatch.setattr(
        stores.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
    )
    vcd.unlink()
    assert stores._materialize_fifo_vcd(fifo, vcd) == fifo


def test_fifo_vcd_materialization_ignores_non_vcd(tmp_path: Path) -> None:
    source = tmp_path / "trace.fifo"
    source.write_text("not a waveform", encoding="utf-8")
    assert stores._materialize_fifo_vcd(source, tmp_path / "trace.vcd") is None


def test_fifo_detection_rejects_empty_and_fifo(tmp_path: Path) -> None:
    empty = tmp_path / "empty.vcd"
    empty.touch()
    assert stores._looks_like_vcd(empty) is False
    fifo = tmp_path / "trace.fifo"
    os.mkfifo(fifo)
    assert stores._looks_like_vcd(fifo) is False
    assert stores._looks_like_vcd(tmp_path / "missing.vcd") is False


def test_metadata_probe_reports_missing_binary(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: None)
    inspection = stores.inspect_store(path)
    assert inspection.failure_kind == "missing_binary"


def test_build_timeout_returns_bounded_completed_process(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "trace.vcd"
    source.write_text("$scope module tb $end\n", encoding="utf-8")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("bwave", 900, output=b"out", stderr=b"timed out")

    monkeypatch.setattr(stores.subprocess, "run", timeout)
    result = stores._run_build("bwave", source, tmp_path / "trace.fst", None)

    assert result.returncode == 124
    assert "timed out after" in result.stderr


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
    assert conversion.pid == 123
    assert conversion.return_code == 0
    assert conversion.poll() == 0
    (store.with_name("trace.fst.progress")).write_bytes(b"progress")
    stderr.write_text("native warning", encoding="utf-8")
    assert conversion.progress_size() > 0
    assert conversion.stderr_tail() == "native warning"
    conversion.kill()
    result = conversion.finish()

    assert result.success is True
    assert "wrote trace.fst" in result.events[0]
    assert not fifo.exists()


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
    monkeypatch.setattr(
        stores.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    assert stores.start_streaming_conversion(fifo, store, stderr_path=stderr) is None
    assert not fifo.exists()


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
        tmp_path / "trace.fifo",
        tmp_path / "trace.fst",
        tmp_path / "trace.stderr",
        None,
    )
    monkeypatch.setattr(stores.os, "close", lambda _fd: None)
    result = conversion.finish()

    assert result.success is False
    assert "rc=3" in result.events[-1]


def test_cli_find_trace_publishes_converted_cache_result(tmp_path: Path, monkeypatch) -> None:
    from booley.bwave import cli

    cache = tmp_path / "cache"
    cache.mkdir()
    selected = cache / "trace.fst"
    selected.write_bytes(MINIMAL_FST_BYTES)
    conversion = stores.ConversionResult(
        selected,
        inspection=stores.StoreInspection(selected, stores.StoreStructure.VIABLE),
    )
    monkeypatch.setattr(
        cli,
        "discover_waveform",
        lambda *_args, **_kwargs: stores.DiscoveryResult(selected, conversion=conversion),
    )
    monkeypatch.setattr(cli, "waveform_cache_dir", lambda _work_dir: cache)

    assert cli.find_trace(tmp_path) == tmp_path / "trace.fst"
    assert (tmp_path / "trace.fst").read_bytes() == MINIMAL_FST_BYTES


def test_cli_find_trace_reports_conversion_failure_and_copy_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from booley.bwave import cli

    selected = tmp_path / "trace.vcd"
    selected.write_text("raw", encoding="utf-8")
    conversion = stores.ConversionResult(
        selected,
        failure_kind="conversion_failed",
        detail="conversion failed",
    )
    monkeypatch.setattr(
        cli,
        "discover_waveform",
        lambda *_args, **_kwargs: stores.DiscoveryResult(selected, conversion=conversion),
    )
    monkeypatch.setattr(cli, "waveform_cache_dir", lambda _work_dir: tmp_path / "cache")
    assert cli.find_trace(tmp_path) == selected
    assert "conversion failed" in capsys.readouterr().err

    source = tmp_path / "cache.fst"
    source.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(
        cli.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
    )
    assert cli._publish_discovered_store(tmp_path, source) == source


def test_cli_find_trace_raises_for_ambiguous_directory(tmp_path: Path, monkeypatch) -> None:
    from booley.bwave import cli

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


def test_session_raw_vcd_build_success_and_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    from booley.bwave import sessions

    trace = tmp_path / "trace.vcd"
    trace.write_text("raw", encoding="utf-8")
    output = trace.with_suffix(".fst")
    success = stores.ConversionResult(
        output,
        inspection=stores.StoreInspection(output, stores.StoreStructure.VIABLE),
        events=("built",),
    )
    monkeypatch.setattr(sessions, "convert_vcd", lambda *_args, **_kwargs: success)
    assert sessions._resolve_raw_vcd(trace, True) == output
    assert "built" in capsys.readouterr().out

    failure = stores.ConversionResult(
        output, failure_kind="conversion_failed", detail="bad conversion"
    )
    monkeypatch.setattr(sessions, "convert_vcd", lambda *_args, **_kwargs: failure)
    with pytest.raises(SystemExit, match="could not build"):
        sessions._resolve_raw_vcd(trace, True)
    assert "bad conversion" in output.with_name("trace.fst.stderr").read_text(encoding="utf-8")


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
    assert session.inspect().usable is False


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
