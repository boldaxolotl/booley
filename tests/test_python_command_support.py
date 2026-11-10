"""Contract for the bare-``python`` PATH helper."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from tests.python_command_support import python_path_entry

_CHECKOUT_BOOLEY = Path(__file__).resolve().parents[1] / "src" / "booley"
_IS_WINDOWS = os.name == "nt"


def test_bare_python_runs_the_running_interpreter(tmp_path: Path) -> None:
    """A bare ``python`` on the helper's PATH entry is this interpreter, venv included."""
    # PATH is the shim entry only, so a broken shim fails here instead of
    # falling through to some other ``python`` on the host.
    result = subprocess.run(
        ["python", "-c", "import sys; print(sys.executable); print(sys.prefix)"],
        env={**os.environ, "PATH": python_path_entry(tmp_path / "shim")},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.splitlines() == [sys.executable, sys.prefix]


_BARE_NAMES = ["python"] if _IS_WINDOWS else ["python3", "python"]


@pytest.mark.parametrize("name", _BARE_NAMES)
def test_bare_python_child_runs_the_test_interpreter_and_checkout(name: str) -> None:
    """A bare ``python3``/``python`` child with the suite's default environment is this interpreter."""
    if _IS_WINDOWS and sys.prefix != sys.base_prefix:
        # The venv launcher starts the base interpreter, whose directory
        # Windows searches first; CI runs without a venv and still executes this.
        pytest.skip("Windows venv launcher resolves bare python to the base interpreter")

    result = subprocess.run(
        [name, "-c", "import sys, booley; print(sys.executable); print(booley.__file__)"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    executable, booley_file = result.stdout.splitlines()
    assert executable == sys.executable
    assert Path(booley_file).resolve().is_relative_to(_CHECKOUT_BOOLEY.resolve())


def test_bare_fusesoc_resolves_from_the_test_environment() -> None:
    """A bare ``fusesoc`` is the test environment's console script, not a host copy."""
    expected = Path(sysconfig.get_path("scripts")) / ("fusesoc.exe" if _IS_WINDOWS else "fusesoc")
    if not expected.exists():
        pytest.skip("the test environment does not provide FuseSoC")

    found = shutil.which("fusesoc")
    assert found is not None
    assert os.path.samefile(found, expected)

    result = subprocess.run(
        ["fusesoc", "--version"],
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert importlib.metadata.version("fusesoc") in result.stdout + result.stderr


def test_suite_path_starts_with_the_test_interpreter_entries() -> None:
    """The autouse fixture puts the interpreter shim and scripts directory first on PATH."""
    entries = os.environ["PATH"].split(os.pathsep)

    if _IS_WINDOWS:
        assert entries[:2] == [str(Path(sys.executable).parent), sysconfig.get_path("scripts")]
    else:
        assert (Path(entries[0]) / "python3").is_file()
        assert entries[1] == sysconfig.get_path("scripts")


@pytest.mark.skipif(_IS_WINDOWS, reason="CreateProcess cannot run script decoys")
def test_interpreter_path_outranks_decoys_on_the_base_path(tmp_path: Path) -> None:
    """Decoys first on the base PATH lose to the test interpreter and its console scripts."""
    from tests.python_command_support import test_interpreter_path

    decoys = tmp_path / "decoys"
    decoys.mkdir()
    for name in ("python3", "python", "fusesoc"):
        decoy = decoys / name
        decoy.write_bytes(b"#!/bin/sh\necho decoy\n")
        decoy.chmod(0o755)
    path = test_interpreter_path(tmp_path / "shim", os.pathsep.join([str(decoys), os.defpath]))

    result = subprocess.run(
        ["python3", "-c", "import sys; print(sys.executable)"],
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == sys.executable
    found = shutil.which("fusesoc", path=path)
    assert found is None or Path(found).parent != decoys
