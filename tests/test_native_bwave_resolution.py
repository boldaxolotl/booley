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
    monkeypatch.setattr(paths, "_native_bwave_candidates", list)
    monkeypatch.delenv("BOOLEY_BWAVE_BIN", raising=False)


def test_candidate_wins_over_path(monkeypatch, tmp_path):
    """Package data (and friends) are checked before PATH — PATH is last resort."""
    native = _native(tmp_path / _bwave_name())
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    _native(path_dir / _bwave_name())
    monkeypatch.setattr(paths, "_native_bwave_candidates", lambda: [native])
    monkeypatch.setenv("PATH", str(path_dir))

    assert paths.native_bwave_binary() == native


def test_wrapper_candidate_is_skipped(monkeypatch, tmp_path):
    """A wrapper sitting in a candidate slot must not be mistaken for the binary."""
    wrapper = _wrapper(tmp_path / "wrapper" / "bwave")
    native = _native(tmp_path / "real" / _bwave_name())
    monkeypatch.setattr(paths, "_native_bwave_candidates", lambda: [wrapper, native])

    assert paths.native_bwave_binary() == native


@pytest.mark.skipif(sys.platform == "win32", reason="Windows .exe launchers are binary files")
def test_path_wrapper_is_rejected(monkeypatch, tmp_path):
    """The only `bwave` on PATH is the wrapper — resolving to it would recurse."""
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    _wrapper(path_dir / _bwave_name())
    monkeypatch.setenv("PATH", str(path_dir))

    assert paths.native_bwave_binary() is None


def test_path_native_is_accepted(monkeypatch, tmp_path):
    """A real binary on PATH still counts (older images put one there)."""
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    native = _native(path_dir / _bwave_name())
    monkeypatch.setenv("PATH", str(path_dir))

    assert paths.native_bwave_binary() == native


def test_windows_exe_is_never_read_as_a_script(monkeypatch, tmp_path):
    """A .exe is a binary by extension — don't sniff its bytes for 'python'."""
    monkeypatch.setattr(sys, "platform", "win32")
    exe = tmp_path / "bwave.exe"
    exe.write_bytes(b"MZ")

    assert paths.is_bwave_wrapper(exe) is False


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


class TestIsBwaveWrapper:
    """The wrapper sniffer — the one predicate keeping the two `bwave`s apart."""

    def test_dockerfile_shell_shim(self, tmp_path):
        """Exactly what the sandbox Dockerfile writes to /usr/local/bin/bwave."""
        shim = tmp_path / "bwave"
        shim.write_text('#!/bin/bash\nexec python3 -m booley.bwave.cli "$@"\n')

        assert paths.is_bwave_wrapper(shim) is True

    def test_pip_console_script(self, tmp_path):
        """`pip install booley-rtl` generates a python shebang + import shim."""
        script = tmp_path / "bwave"
        script.write_text(
            "#!/usr/bin/python3\n"
            "# -*- coding: utf-8 -*-\n"
            "import re\nimport sys\n"
            "from booley.bwave.cli import main\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        )

        assert paths.is_bwave_wrapper(script) is True

    def test_native_binary_blob(self, tmp_path):
        """The compiled Rust binary is not a wrapper, however its bytes look."""
        binary = _native(tmp_path / "bwave")

        assert paths.is_bwave_wrapper(binary) is False

    def test_exe_suffix_short_circuits(self, tmp_path):
        """Extension wins over content sniffing — a .exe is never a shim."""
        exe = tmp_path / "bwave.exe"
        exe.write_bytes(b"MZ\x90\x00 booley.bwave.cli")

        assert paths.is_bwave_wrapper(exe) is False

    def test_missing_file_is_not_a_wrapper(self, tmp_path):
        """An unreadable/absent path is simply not a wrapper (caller checks exists)."""
        assert paths.is_bwave_wrapper(tmp_path / "nope") is False


def test_missing_binary_resolves_to_none(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

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
    from booley.sim import bwave_fifo

    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    _wrapper(local_bin / _bwave_name())
    native = _native(tmp_path / ".cargo" / "bin" / _bwave_name())

    monkeypatch.setattr(paths, "_native_bwave_candidates", lambda: [native])
    monkeypatch.setenv("PATH", str(local_bin))

    assert bwave_fifo._find_bwave_bin() == str(native)
    assert bwave_fifo.can_stream_bwave_fifo() is (os.name == "posix")


@pytest.mark.skipif(sys.platform == "win32", reason="FIFO streaming is POSIX-only")
def test_fifo_streaming_declines_when_only_the_wrapper_exists(monkeypatch, tmp_path):
    from booley.sim import bwave_fifo

    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    _wrapper(path_dir / _bwave_name())
    monkeypatch.setenv("PATH", str(path_dir))

    assert bwave_fifo._find_bwave_bin() is None
    assert bwave_fifo.can_stream_bwave_fifo() is False
