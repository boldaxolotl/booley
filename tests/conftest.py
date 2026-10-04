"""Shared fixtures for infrastructure unit tests."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

pytest_plugins = ["tests.timeout_evidence", "tests.timeout_headroom"]


@pytest.fixture
def isolated_git_attributes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Attribute boundary tests select their own policy instead of the host's."""
    monkeypatch.setenv("GIT_ATTR_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "isolated-git.config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "isolated-xdg"))


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
    # Imported here: pytest_plugins must load it first for assertion rewriting.
    from tests.timeout_headroom import timeout_plugin_loaded

    _isolate_xdist_worker_temp()
    if timeout_plugin_loaded(config):
        from pytest_timeout import get_env_settings

        if get_env_settings(config).timeout is None:
            config.option.timeout = _DEFAULT_TEST_TIMEOUT_S


def pytest_unconfigure(config: pytest.Config) -> None:
    """Remove the worker-only temp root after its tests and children exit."""
    del config  # hook signature; cleanup uses the module-owned path
    global _XDIST_WORKER_TEMP

    if _XDIST_WORKER_TEMP is not None:
        shutil.rmtree(_XDIST_WORKER_TEMP)
        _XDIST_WORKER_TEMP = None
        tempfile.tempdir = None


_FLOW_SESSIONS = pytest.StashKey[list[Any]]()


@pytest.fixture(autouse=True)
def _track_flow_sessions(request: pytest.FixtureRequest) -> Iterator[None]:
    """Record each Flow session a test creates for the teardown lock check below.

    Minimal CI environments (for example the production-image smoke jobs) lack
    Flow dependencies such as PyYAML. No Flow session can exist there, so the
    fixture tracks nothing instead of failing every test's setup.
    """
    try:
        from booley.flows.flow_session import FlowSession
    except ModuleNotFoundError:
        yield
        return

    sessions = request.node.stash.setdefault(_FLOW_SESSIONS, [])
    initialize = FlowSession.__init__

    def tracking_init(session: FlowSession, *args: Any, **kwargs: Any) -> None:
        initialize(session, *args, **kwargs)
        sessions.append(session)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FlowSession, "__init__", tracking_init)
        yield


def _publication_leaks(item: pytest.Item, sessions: list[Any]) -> list[str]:
    """Observe callbacks and locks before emergency cleanup can hide a leak."""
    # ExitStack has no public callback-count API; this is test-only introspection.
    leaks = [
        f"Flow session {session.name!r} ({id(session):#x}) has publication callbacks"
        for session in sessions
        if session.publication_resources._exit_callbacks
    ]
    tmp_path = getattr(item, "funcargs", {}).get("tmp_path")
    if sys.platform != "win32" and isinstance(tmp_path, Path):
        from tests.file_lock_probe import invocation_lock_paths, lock_is_held

        leaks.extend(
            f"Simulation invocation lock still held: {path}"
            for path in sorted(invocation_lock_paths(tmp_path))
            if lock_is_held(path)
        )
    return leaks


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Iterator[None]:
    """Detect leaked publication resources, then close every tracked session.

    Direct Flow calls must own their publication scope. Observe leaks after all
    fixture finalizers so test patches of Path or fcntl are no longer active.
    Emergency cleanup still runs when a finalizer or the leak probe fails.
    """
    try:
        return (yield)
    finally:
        sessions = item.stash.get(_FLOW_SESSIONS, [])
        try:
            leaks = _publication_leaks(item, sessions)
        finally:
            with ExitStack() as cleanup:
                for session in sessions:
                    cleanup.callback(session.publication_resources.close)
        if leaks:
            pytest.fail("Publication resources leaked past the test:\n" + "\n".join(leaks))


@pytest.fixture(autouse=True)
def _isolate_host_lifecycle_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host-global mutation locks local to each test process."""
    from booley.runtime import lifecycle_lock

    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path / "host-config")


@pytest.fixture(autouse=True)
def _isolate_agent_session_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite may run from a Claude or Codex shell; start every test outside one.

    ``agent_session_app`` changes Doctor and CLI behavior when these markers are
    set, so an inherited marker made clean-run Doctor tests fail only locally.
    Tests of that detection set the markers they need explicitly.
    """
    from booley.runtime import runtime_context

    for markers in dict(runtime_context._AGENT_SESSION_MARKERS).values():
        for marker in markers:
            monkeypatch.delenv(marker, raising=False)


# --- Minimal FST fixtures ---------------------------------------------------
# Structural FST inspection requires a well-formed header block AND at least one
# value-change block: a header-only file is the exact shape a simulator writes
# when it was asked to trace via a CLI convention its main() does not
# implement, and accepting it turned an untraced run into a passing one (the
# 443-byte Ibex false pass). Tests that need a *valid* store must therefore
# carry data, not just a header.
FST_HEADER_BYTES = bytes([0]) + (329).to_bytes(8, "big") + b"\x00" * 321
FST_VCDATA_BYTES = bytes([1]) + (16).to_bytes(8, "big") + b"\x00" * 8
#: Smallest byte string that reads as a real, queryable waveform store.
MINIMAL_FST_BYTES = FST_HEADER_BYTES + FST_VCDATA_BYTES
