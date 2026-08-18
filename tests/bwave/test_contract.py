"""Cross-process pins for the bwave exit-code / stderr-marker contract.

``booley.bwave.contract`` documents what the Rust binary promises;
these tests run the *built binary* and assert the promise holds, so a
reworded Rust diagnostic or a reshuffled exit code fails here instead of
silently breaking a Python consumer (coverage_analyst's discovery fallback,
bwave_sessions' identity probe). The Rust side pins the same markers in
``crates/bwave/src/cache.rs`` (contract tests at the bottom of its test mod).
"""

from __future__ import annotations

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
)

BOOLEY_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = BOOLEY_ROOT / "crates" / "bwave" / "tests" / "fixtures"

# A VCD that declares no signals — the header-only shape a Verilator sim
# traced via the auto-generated --main produces.
EMPTY_VCD = (
    "$timescale 1ns $end\n$scope module tb $end\n$upscope $end\n$enddefinitions $end\n#0\n#10\n"
)


def _native_bwave_binary() -> Path:
    """The Rust binary from the local cargo tree, building it if needed."""
    suffix = ".exe" if sys.platform == "win32" else ""
    release = BOOLEY_ROOT / "crates" / "bwave" / "target" / "release" / f"bwave{suffix}"
    debug = BOOLEY_ROOT / "crates" / "bwave" / "target" / "debug" / f"bwave{suffix}"
    if release.exists():
        return release
    if debug.exists():
        return debug
    try:
        subprocess.run(
            [
                "cargo",
                "build",
                "--manifest-path",
                str(BOOLEY_ROOT / "crates" / "bwave" / "Cargo.toml"),
            ],
            check=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"native bwave binary not built and cargo unavailable: {exc}")
    return debug


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_native_bwave_binary()), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small real store built from a fixture VCD."""
    vcd = FIXTURE_DIR / "test_basic.vcd"
    if not vcd.exists():
        pytest.skip("test_basic.vcd fixture missing")
    out = tmp_path_factory.mktemp("contract") / "basic.fst"
    result = _run("build", str(vcd), "-o", str(out))
    assert result.returncode == EXIT_OK, result.stderr
    return out


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
