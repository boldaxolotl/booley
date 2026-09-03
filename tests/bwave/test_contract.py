"""Cross-process pins for the native B-Wave/Python contract.

``booley.bwave.contract`` documents what the Rust binary promises;
these tests run the *built binary* and assert the promise holds, so a
metadata or diagnostic change fails here instead of silently breaking a Python
consumer (TraceSession, coverage_analyst's discovery fallback, bwave_sessions'
identity probe). The Rust side pins the same markers in ``crates/bwave/src/cache.rs``
(contract tests at the bottom of its test mod).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from booley.bwave.contract import (
    EXIT_ENV,
    EXIT_OK,
    EXIT_USAGE,
    NO_MATCH_MARKER,
    NO_SIGNALS_IN_STORE_MARKER,
    SCOPE_LINE_PREFIX,
    BWaveListMetadata,
    decode_list_metadata,
)
from booley.flows.sim.trace_session import TraceSession

BOOLEY_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = BOOLEY_ROOT / "crates" / "bwave" / "tests" / "fixtures"
PYTHON_FIXTURE_DIR = Path(__file__).with_name("fixtures")
pytestmark = pytest.mark.native_bwave

# A VCD that declares no signals — the header-only shape a Verilator sim
# traced via the auto-generated --main produces.
EMPTY_VCD = (
    "$timescale 1ns $end\n$scope module tb $end\n$upscope $end\n$enddefinitions $end\n#0\n#10\n"
)


def _native_bwave_binary() -> Path:
    """Return a prebuilt Rust binary or skip on a stock development host."""
    suffix = ".exe" if sys.platform == "win32" else ""
    release = BOOLEY_ROOT / "crates" / "bwave" / "target" / "release" / f"bwave{suffix}"
    debug = BOOLEY_ROOT / "crates" / "bwave" / "target" / "debug" / f"bwave{suffix}"
    if release.exists():
        return release
    if debug.exists():
        return debug
    pytest.skip(
        "native bwave binary not built; run cargo build --manifest-path crates/bwave/Cargo.toml"
    )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_native_bwave_binary()), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _build_store(vcd: Path, out: Path) -> Path:
    result = _run("build", str(vcd), "-o", str(out))
    assert result.returncode == EXIT_OK, result.stderr
    assert out.exists(), "successful build did not create an FST store"
    return out


def _list_metadata(store: Path) -> tuple[BWaveListMetadata, list[object]]:
    result = _run("list", str(store), "--format", "json", "--limit", "1")
    assert result.returncode == EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    return decode_list_metadata(result.stdout), payload["data"]["signals"]


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small real store built from a fixture VCD."""
    vcd = FIXTURE_DIR / "test_basic.vcd"
    out = tmp_path_factory.mktemp("contract") / "basic.fst"
    return _build_store(vcd, out)


@pytest.fixture(scope="module")
def multi_root_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real store with two independent root scopes."""
    vcd = PYTHON_FIXTURE_DIR / "test_multi_root.vcd"
    out = tmp_path_factory.mktemp("contract-multi-root") / "multi-root.fst"
    return _build_store(vcd, out)


def test_native_list_metadata_crosses_single_root_python_decoder(store: Path) -> None:
    metadata, signals = _list_metadata(store)

    assert metadata.scope_prefix == "tb"
    assert metadata.root_scopes == ("tb",)
    assert metadata.signal_count == 3
    assert metadata.total_ticks == 185
    assert len(signals) == 1


def test_native_list_metadata_crosses_multi_root_python_decoder(
    multi_root_store: Path,
) -> None:
    """Regression for 7146fc1d: multiple roots are not an empty store."""
    metadata, signals = _list_metadata(multi_root_store)

    assert metadata.scope_prefix == ""
    assert metadata.root_scopes == ("$rootio", "uart16550")
    assert metadata.signal_count == 3
    assert metadata.total_ticks == 5
    assert len(signals) == 1


def test_trace_session_accepts_native_multi_root_store(
    multi_root_store: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "run"
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(
        "booley.flows.sim.bwave_fifo._find_bwave_bin",
        lambda: str(_native_bwave_binary()),
    )
    monkeypatch.setattr(
        "booley.flows.sim.trace_session._bwave_cache_root",
        lambda: cache_root,
    )

    inspection = TraceSession(work_dir, trace_scope="uart16550").inspect(multi_root_store)

    assert inspection.usable is True
    assert inspection.artifact is not None
    assert inspection.artifact.top_scope == "$rootio, uart16550"
    assert inspection.artifact.signal_count == 3
    assert inspection.artifact.total_ticks == 5
    status = json.loads((work_dir / "trace_status.json").read_text(encoding="utf-8"))
    assert status["current_status"] == "usable"
    assert status["trace_metadata"]["top_scope"] == "$rootio, uart16550"
    assert status["trace_metadata"]["signal_count"] == 3
    assert status["trace_metadata"]["total_ticks"] == 5


def test_total_miss_is_exit_usage_plus_marker(store: Path) -> None:
    """The exact (returncode, stderr-substring) tuple coverage_analyst keys on."""
    result = _run("stats", str(store), "-s", "no_such_signal_anywhere")
    assert result.returncode == EXIT_USAGE, result.stderr
    assert NO_MATCH_MARKER in result.stderr.lower(), result.stderr


def test_list_tree_stderr_carries_the_scope_line(store: Path) -> None:
    """bwave_sessions._trace_identity parses `# scope: <top>` off stderr."""
    result = _run("list", str(store), "--tree")
    assert result.returncode == EXIT_OK, result.stderr
    scope_lines = [
        line for line in result.stderr.splitlines() if line.startswith(SCOPE_LINE_PREFIX)
    ]
    assert scope_lines, f"no scope line on stderr:\n{result.stderr}"
    assert scope_lines[0][len(SCOPE_LINE_PREFIX) :].strip(), "scope line names no scope"


def test_build_refuses_zero_signal_vcd(tmp_path: Path) -> None:
    """Producer-side loud-fail: a header-only store must not build silently."""
    vcd = tmp_path / "empty.vcd"
    vcd.write_text(EMPTY_VCD, encoding="utf-8")
    out = tmp_path / "empty.fst"
    result = _run("build", str(vcd), "-o", str(out))
    assert result.returncode == EXIT_USAGE, result.stderr
    assert "declares no signals" in result.stderr, result.stderr
    assert not out.exists(), "refusal must not leave a store file behind"


def test_empty_store_marker_survives_in_binary() -> None:
    """NO_SIGNALS_IN_STORE_MARKER must appear in the binary's diagnostic.

    A header-only store can no longer be produced via `build`, so this pins
    the marker through the compiled-in message rather than a live query (the
    Rust integration suite covers the live query path in-process).
    """
    binary = _native_bwave_binary().read_bytes()
    assert NO_SIGNALS_IN_STORE_MARKER.encode() in binary, (
        "the empty-store diagnostic (cache.rs no_signals_in_store_message) "
        f"no longer contains the pinned marker {NO_SIGNALS_IN_STORE_MARKER!r}"
    )


def test_env_errors_stay_exit_env(tmp_path: Path) -> None:
    """Exit 1 is reserved for environment/I-O: an unreadable store is not usage."""
    missing = tmp_path / "does_not_exist.fst"
    result = _run("stats", str(missing), "-s", "*")
    assert result.returncode == EXIT_ENV, result.stderr
