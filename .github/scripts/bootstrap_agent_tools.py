#!/usr/bin/env python3
"""Create one fingerprinted, tools-only environment at its final path.

A virtual environment is not relocatable: pip bakes the interpreter's absolute
path into every console-script launcher (POSIX shebangs and Windows ``.exe``
launchers alike) and ``venv`` bakes it into the activation scripts. The
environment is therefore built in place at its final path, never renamed into
it. ``receipt.json`` is the publication marker: it is written atomically, last,
and only after every probe passes, so readers never accept a partial build.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.dev_support import agent_readiness as readiness

VENV_TIMEOUT_SECONDS = 120
PIP_TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 60
# One generous bound: twice a worst-case build (venv, pip install, two probes).
LOCK_WAIT_SECONDS = 2 * (VENV_TIMEOUT_SECONDS + PIP_TIMEOUT_SECONDS + 2 * PROBE_TIMEOUT_SECONDS)


def main(argv: list[str] | None = None) -> int:
    """Build and publish the environment unless its receipt is already valid."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    environment: Path = args.environment
    environment.parent.mkdir(parents=True, exist_ok=True)
    # An OS lock: the kernel releases it when a builder dies, so nothing goes stale.
    with environment.with_name(f".{environment.name}.lock").open("a+", encoding="utf-8") as lock:
        wait_for_file_lock(lock, timeout_s=LOCK_WAIT_SECONDS)
        try:
            python = readiness.venv_python(environment)
            if not readiness.validate_environment(environment, args.fingerprint, python)[0]:
                _rebuild(environment, root, args.fingerprint, readiness.PINNED_RUNNERS)
        finally:
            release_file_lock(lock)
    return 0


def _rebuild(environment: Path, root: Path, fingerprint: str, pins: dict[str, str]) -> None:
    """Replace a rejected tree with a fresh build at the final path.

    The caller holds the lock. A failed build leaves neither a tree nor a
    receipt and re-raises its own error; a failed cleanup is noted on it.
    """
    _remove(environment)
    try:
        _build(environment, root, fingerprint, pins)
    except BaseException as error:
        try:
            _remove(environment)
        except OSError as cleanup_error:
            error.add_note(f"partial agent-tools tree {environment} remains: {cleanup_error}")
        raise


def _remove(environment: Path) -> None:
    """Unpublish, then delete, a tree; a symlink or file is only unlinked."""
    if environment.is_dir() and not environment.is_symlink():
        (environment / "receipt.json").unlink(missing_ok=True)
        shutil.rmtree(environment)
    else:
        environment.unlink(missing_ok=True)


def _build(environment: Path, root: Path, fingerprint: str, pins: dict[str, str]) -> None:
    """Create the environment at its final path and publish its receipt last."""
    subprocess.run(
        (sys.executable, "-B", "-m", "venv", str(environment)),
        cwd=root,
        check=True,
        timeout=VENV_TIMEOUT_SECONDS,
    )
    python = readiness.venv_python(environment)
    subprocess.run(
        (str(python), "-B", "-m", "pip", *readiness.dependency_argv(root)),
        cwd=root,
        check=True,
        timeout=PIP_TIMEOUT_SECONDS,
    )
    subprocess.run(
        (str(python), "-B", "-m", "pip", "check"),
        cwd=root,
        check=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    installed = _installed(python, root)
    if any(installed.get(name) != version for name, version in pins.items()):
        raise RuntimeError("bootstrap did not install the exact runner pins")
    receipt = {
        "schema_version": readiness.RECEIPT_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "python": sys.version_info[:3],
        "platform": sys.platform,
        "installed": installed,
    }
    # Write-then-rename keeps the publication marker all-or-nothing.
    pending = environment / f".receipt.json.{uuid.uuid4().hex}"
    pending.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    pending.replace(environment / "receipt.json")


def _installed(python: Path, root: Path) -> dict[str, str]:
    code = "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name'].lower(): d.version for d in m.distributions()}))"
    result = subprocess.run(
        (str(python), "-B", "-c", code),
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("shared environment returned an invalid distribution set")
    return value


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
