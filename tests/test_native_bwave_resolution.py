"""Resolution of the *native* (Rust) bwave binary — booley.runtime.paths.

Two executables answer to the name `bwave`: the native binary and the Python
wrapper (booley.bwave.cli). Only the wrapper is on PATH in the sandbox image,
so a human typing `bwave gui` reaches the verb they mean — which means every
caller that wants the *binary* (FIFO streaming, coverage stats, the wrapper's
own query forwarding) has to resolve it here and skip the wrapper. Get this
wrong and the wrapper resolves to itself, or the simulator is handed a script
that exits without draining the trace FIFO.
"""

from __future__ import annotations

import os
import sys

import pytest

from booley.runtime import paths


def _bwave_name() -> str:
    return "bwave.exe" if sys.platform == "win32" else "bwave"


def _native(path):
    """A stand-in for the compiled binary (ELF magic, not a script)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF native bwave placeholder")
    path.chmod(0o755)  # shutil.which only sees executables
    return path


def _wrapper(path):
    """A stand-in for the Python wrapper shim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env python3\nimport booley.bwave.cli\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _no_ambient_candidates(monkeypatch):
    """Isolate from the dev host's real cargo build / installed package data."""
    # Private: the real list names the checkout's cargo build and the image's static paths,
    # which a dev host may hold, so only a stated list makes the order observable.
    monkeypatch.setattr(paths, "_native_bwave_candidates", list)
    monkeypatch.delenv("BOOLEY_BWAVE_BIN", raising=False)


def test_candidate_wins_over_path(monkeypatch, tmp_path):
    """Package data (and friends) are checked before PATH — PATH is last resort."""
    native = _native(tmp_path / _bwave_name())
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    _native(path_dir / _bwave_name())
    # Private: a stated candidate list, as in the autouse fixture above.
    monkeypatch.setattr(paths, "_native_bwave_candidates", lambda: [native])
    monkeypatch.setenv("PATH", str(path_dir))

    assert paths.native_bwave_binary() == native


def test_wrapper_candidate_is_skipped(monkeypatch, tmp_path):
    """A wrapper sitting in a candidate slot must not be mistaken for the binary."""
    wrapper = _wrapper(tmp_path / "wrapper" / "bwave")
    native = _native(tmp_path / "real" / _bwave_name())
    # Private: a stated candidate list, as in the autouse fixture above.
    monkeypatch.setattr(paths, "_native_bwave_candidates", lambda: [wrapper, native])

    assert paths.native_bwave_binary() == native


def test_path_native_is_accepted(monkeypatch, tmp_path):
    """A real binary on PATH still counts (older images put one there)."""
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    native = _native(path_dir / _bwave_name())
    monkeypatch.setenv("PATH", str(path_dir))

    assert paths.native_bwave_binary() == native


def test_windows_exe_on_path_is_accepted(monkeypatch, tmp_path):
    """A PATH hit on bwave.exe is the binary — accept it (Windows dev boxes)."""
    monkeypatch.setattr(sys, "platform", "win32")
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    exe = path_dir / "bwave.exe"
    exe.write_bytes(b"MZ")
    exe.chmod(0o755)
    monkeypatch.setattr(paths.shutil, "which", lambda _name: str(exe))

    assert paths.native_bwave_binary() == exe


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        # What the sandbox Dockerfile writes to /usr/local/bin/bwave.
        ("bwave", b'#!/bin/bash\nexec python3 -m booley.bwave.cli "$@"\n', True),
        # What `pip install booley-rtl` generates: a python shebang and import shim.
        ("bwave", b"#!/usr/bin/python3\nimport sys\nfrom booley.bwave.cli import main\n", True),
        # The compiled binary, however its bytes look.
        ("bwave", b"\x7fELF native bwave placeholder", False),
        # The extension wins over content sniffing: a .exe is never a shim.
        ("bwave.exe", b"MZ\x90\x00 booley.bwave.cli", False),
        # An absent path is simply not a wrapper (the caller checks existence).
        ("nope", None, False),
    ],
    ids=["shell-shim", "console-script", "binary", "exe", "missing"],
)
def test_is_bwave_wrapper(tmp_path, name, content, expected):
    path = tmp_path / name
    if content is not None:
        path.write_bytes(content)

    assert paths.is_bwave_wrapper(path) is expected


@pytest.mark.parametrize(
    "on_path",
    [
        # Resolving to the wrapper would recurse.
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="Windows .exe launchers are binary files"
            ),
            id="only-the-wrapper",
        ),
        pytest.param(False, id="nothing"),
    ],
)
def test_path_without_a_native_binary_resolves_to_none(monkeypatch, tmp_path, on_path):
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    if on_path:
        _wrapper(path_dir / _bwave_name())
    monkeypatch.setenv("PATH", str(path_dir))

    assert paths.native_bwave_binary() is None


def test_env_override_takes_precedence(monkeypatch, tmp_path):
    """BOOLEY_BWAVE_BIN is the escape hatch when resolution guesses wrong."""
    monkeypatch.undo()  # drop the autouse candidate stub; exercise the real list
    override = _native(tmp_path / _bwave_name())
    monkeypatch.setenv("BOOLEY_BWAVE_BIN", str(override))

    assert paths.native_bwave_binary() == override


def test_fifo_streaming_prefers_the_binary_over_a_stale_path_wrapper(monkeypatch, tmp_path):
    """The FIFO streamer must get the binary, never the wrapper (it would not drain).

    A stale `pip install --user` wrapper in ~/.local/bin is the classic PATH hit;
    the candidate list has to beat it.
    """
    from booley.bwave import waveform_store

    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    _wrapper(local_bin / _bwave_name())
    native = _native(tmp_path / ".cargo" / "bin" / _bwave_name())

    # Private: a stated candidate list, as in the autouse fixture above.
    monkeypatch.setattr(paths, "_native_bwave_candidates", lambda: [native])
    monkeypatch.setenv("PATH", str(local_bin))

    assert waveform_store.native_bwave_binary() == str(native)
    assert waveform_store.can_stream_waveform() is (os.name == "posix")


@pytest.mark.skipif(sys.platform == "win32", reason="FIFO streaming is POSIX-only")
def test_fifo_streaming_declines_when_only_the_wrapper_exists(monkeypatch, tmp_path):
    from booley.bwave import waveform_store

    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    _wrapper(path_dir / _bwave_name())
    monkeypatch.setenv("PATH", str(path_dir))

    assert waveform_store.native_bwave_binary() is None
    assert waveform_store.can_stream_waveform() is False
