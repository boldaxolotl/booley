"""Shared fixtures for infrastructure unit tests."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path, PureWindowsPath

import pytest

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def symlink_or_skip(link: Path, target: Path | str, **kwargs: object) -> None:
    """``link.symlink_to(target)``, skipping the test if the OS forbids it.

    Real symlink creation needs a privilege a stock Windows host lacks without
    Developer Mode / an elevated shell (``[WinError 1314] A required privilege
    is not held``). These code paths genuinely require symlinks, so skipping is
    honest — and lossless where they run in-container (Linux) in production.
    """
    try:
        link.symlink_to(target, **kwargs)  # type: ignore[arg-type]
    except OSError as exc:  # WinError 1314: needs Developer Mode / admin
        pytest.skip(f"symlink creation unavailable on this host: {exc}")


def require_symlinks(tmp_path: Path) -> None:
    """Skip the test when this host cannot create symlinks.

    For tests that reach a *production* code path which creates (or, like
    ``deploy_skills``, silently swallows a failure to create) symlinks — probe
    up front rather than asserting on the swallowed outcome.
    """
    probe = tmp_path / "__symlink_probe__"
    try:
        probe.symlink_to(tmp_path)
    except OSError as exc:  # WinError 1314: needs Developer Mode / admin
        pytest.skip(f"symlink creation unavailable on this host: {exc}")
    else:
        probe.unlink()


# Generous per-test ceiling: only true hangs (a wedged subprocess or an
# unbounded stall loop) exceed it, so it never flakes a healthy test.
_DEFAULT_TEST_TIMEOUT_S = 120
_XDIST_WORKER_TEMP: Path | None = None


def _xdist_worker_temp_base() -> Path:
    """Return the parent directory for worker-specific temporary roots."""
    base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    if sys.platform != "win32":
        return base

    workspace = Path.cwd()
    if PureWindowsPath(base).drive.casefold() != PureWindowsPath(workspace).drive.casefold():
        # FuseSoC's Edalizer relativizes core files against its workspace and
        # Windows refuses to relativize across volumes. Keep tmp_path fixtures
        # on the checkout's drive when the system temp directory is elsewhere.
        return workspace
    return base


def _isolate_xdist_worker_temp() -> None:
    """Give each xdist worker a subprocess-visible temporary directory.

    Some production paths intentionally persist runtime-local state beneath
    ``tempfile.gettempdir()``. Separate xdist processes must not share those
    files: tests in one worker otherwise delete or replace another worker's
    B-Wave session registry. Updating both ``tempfile.tempdir`` and the
    platform temp environment keeps in-process code and child Python/Rust
    commands on the same worker-specific root.
    """
    global _XDIST_WORKER_TEMP

    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    run_id = os.environ.get("PYTEST_XDIST_TESTRUNUID")
    if not worker_id or not run_id:
        return

    base = _xdist_worker_temp_base()
    worker_temp = base / f"booley-pytest-{run_id}-{worker_id}"
    worker_temp.mkdir(parents=True, exist_ok=True)
    for variable in ("TMPDIR", "TEMP", "TMP"):
        os.environ[variable] = str(worker_temp)
    tempfile.tempdir = str(worker_temp)
    _XDIST_WORKER_TEMP = worker_temp


def pytest_configure(config: pytest.Config) -> None:
    """Bound every test's wall-clock so a hang fails loudly instead of wedging
    the whole suite.

    Requires the ``pytest-timeout`` dev dependency. When that plugin is absent
    this is a deliberate no-op that emits *no* config warning, keeping the suite
    warning-free (principle 12) in bare environments.
    """
    _isolate_xdist_worker_temp()
    if config.pluginmanager.hasplugin("pytest_timeout") and not config.option.timeout:
        config.option.timeout = _DEFAULT_TEST_TIMEOUT_S


def pytest_unconfigure(config: pytest.Config) -> None:
    """Remove the worker-only temp root after its tests and children exit."""
    del config  # hook signature; cleanup uses the module-owned path
    global _XDIST_WORKER_TEMP

    if _XDIST_WORKER_TEMP is not None:
        shutil.rmtree(_XDIST_WORKER_TEMP)
        _XDIST_WORKER_TEMP = None
        tempfile.tempdir = None


@pytest.fixture(autouse=True)
def _isolate_host_lifecycle_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host-global mutation locks local to each test process."""
    from booley.harness import lifecycle_lock

    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path / "host-config")


# --- Minimal FST fixtures ---------------------------------------------------
# ``_bwave_valid`` requires a well-formed header block AND at least one
# value-change block: a header-only file is the exact shape a simulator writes
# when it was asked to trace via a CLI convention its main() does not
# implement, and accepting it turned an untraced run into a passing one (the
# 443-byte Ibex false pass). Tests that need a *valid* store must therefore
# carry data, not just a header.
FST_HEADER_BYTES = bytes([0]) + (329).to_bytes(8, "big") + b"\x00" * 321
FST_VCDATA_BYTES = bytes([1]) + (16).to_bytes(8, "big") + b"\x00" * 8
#: Smallest byte string that reads as a real, queryable waveform store.
MINIMAL_FST_BYTES = FST_HEADER_BYTES + FST_VCDATA_BYTES
