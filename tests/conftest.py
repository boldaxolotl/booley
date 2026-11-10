"""Shared fixtures for infrastructure unit tests."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Generator, Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO

import pytest

from tests.python_command_support import interpreter_first_path, python_path_entry

_SOURCE_ROOT = Path(__file__).resolve().parents[1]

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(_SOURCE_ROOT / "src"))

pytest_plugins = ["tests.timeout_evidence", "tests.timeout_headroom", "tests.sandbox_only"]

# Temporary (#3329): a bare ``pytest tests/`` skips the Goal Mode unit tests; the
# coverage omit for ``src/booley/goals`` (pyproject.toml) and the classifier
# exemption (.github/scripts/ci_goal_tests.py) go with it. Remove all three when
# Goal Mode is removed (#3276), or when metrics-driven development fully replaces
# it. Naming the path still collects them: ``pytest tests/goals``.
collect_ignore = ["goals"]


@pytest.fixture(autouse=True)
def adapter_subprocesses_import_this_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A launched ``python3 -m booley.flows.sim.backends.*`` must run this checkout."""
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, (str(_SOURCE_ROOT / "src"), os.environ.get("PYTHONPATH")))),
    )


# Git settings every Git child of the suite runs with, exported as command-scope
# configuration (``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n``).
#
# Why: a Git command can detach ``git maintenance`` / ``gc`` that outlives its test
# and keeps ``objects/maintenance.lock`` open in the test's directory (#3120), which
# breaks cleanup (WinError 32 on Windows) and leaks processes.
#
# How: command scope outranks every file scope, so the setting holds whichever
# global/system file a test selects with ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_NOSYSTEM``.
# ``pytest_configure`` exports it before workers start, and ``isolated_git_excludes``,
# ``reset_git_command_config`` and ``add_git_command_config`` keep it when a test
# rewrites command scope.
#
# Known gaps (not covered by this setting):
# - ``src/booley/goals/committed_bytes.py`` replaces command scope with
#   ``GIT_CONFIG_COUNT=0`` / ``GIT_CONFIG_PARAMETERS=""`` for its shadow-repository Git.
# - ``src/booley/runtime/incontainer_git_identity_cleanup.py`` strips every
#   ``GIT_CONFIG_*`` for its ``git config --file`` reads.
# - ``src/booley/runtime/incontainer_git_identity.py`` defines the Sandbox container's
#   own ``GIT_CONFIG_COUNT=2`` identity environment; it governs Git inside a Sandbox,
#   not the host children the suite starts.
# - Git itself drops ``GIT_CONFIG_COUNT``/``GIT_CONFIG_PARAMETERS`` for the receiving
#   process on local-path transports (``local_repo_env``), so ``receive-pack`` /
#   ``upload-pack`` of a push or fetch to a local path runs without these settings.
# - An inherited ``GIT_CONFIG_PARAMETERS`` (what ``git -c`` exports) is read after the
#   numbered entries, so one that sets these keys overrides them.
# - Test helpers build a child environment from scratch instead of inheriting it, for
#   example ``tests/dev_support/test_pre_push_hook.py::_git``,
#   ``tests/qa/test_uart_evaluator.py`` and
#   ``tests/runtime/test_incontainer_git_identity.py``. ``sandbox_git`` in
#   ``tests/harness/test_doctor_sandbox_git_identity.py`` clears command scope because
#   it needs exact contents.
_SESSION_GIT_COMMAND_CONFIG: tuple[tuple[str, str], ...] = (
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
)


def _git_command_entries(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return the ``(key, value)`` command-scope pairs Git reads from ``environ``.

    Git reads an empty ``GIT_CONFIG_COUNT`` as 0. A non-numeric count raises
    ``ValueError`` and a count above the entries present raises ``KeyError``, just
    as Git rejects both.
    """
    count = int(environ.get("GIT_CONFIG_COUNT") or "0")
    return [
        (environ[f"GIT_CONFIG_KEY_{index}"], environ[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(count)
    ]


def _disable_git_auto_maintenance_for_children() -> None:
    """Append the session settings to the command scope child processes inherit.

    Idempotent: a key whose effective (last) value already is the wanted one is not
    appended again, so repeated ``pytest_configure`` calls (mutmut) and xdist workers
    re-running it on an inherited environment add nothing. Inherited entries stay.
    """
    entries = _git_command_entries(os.environ)
    for key, value in _SESSION_GIT_COMMAND_CONFIG:
        effective = [entry_value for name, entry_value in entries if name.lower() == key]
        if effective and effective[-1] == value:
            continue
        os.environ[f"GIT_CONFIG_KEY_{len(entries)}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{len(entries)}"] = value
        entries.append((key, value))
    os.environ["GIT_CONFIG_COUNT"] = str(len(entries))


def add_git_command_config(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    """Append ``key=value`` to the command scope, keeping the entries already there.

    The last entry for a key wins, so the test's value takes effect while the
    session's auto-maintenance settings stay in place.
    """
    index = len(_git_command_entries(os.environ))
    monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
    monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(index + 1))


def reset_git_command_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the command scope exactly the session's settings, dropping inherited ones.

    For fixtures that isolate the host's inherited policy: inherited entries beyond
    the session's are ignored once ``GIT_CONFIG_COUNT`` is lowered. Leaves
    ``GIT_CONFIG_PARAMETERS`` alone.
    """
    for index, (key, value) in enumerate(_SESSION_GIT_COMMAND_CONFIG):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(_SESSION_GIT_COMMAND_CONFIG)))


@pytest.fixture(scope="session")
def _python_shim_entry(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Write the bare ``python``/``python3`` shims once per session (xdist worker).

    Returns the PATH entry on which both names run the test interpreter.
    """
    return python_path_entry(tmp_path_factory.mktemp("python-shim"))


@pytest.fixture(autouse=True)
def subprocesses_run_the_test_interpreter(
    monkeypatch: pytest.MonkeyPatch, _python_shim_entry: str
) -> None:
    """A launched bare ``python3``, ``python`` or ``fusesoc`` must come from the test interpreter.

    Production launches adapters as ``python3 -m booley...`` and FuseSoC as
    ``fusesoc``; both resolve from ``PATH``, which on a developer host finds the
    system interpreter and a user-site FuseSoC. Putting the interpreter shims and
    its scripts directory first makes children independent of the host. Tests that
    set their own ``PATH`` keep precedence. Like the ``PYTHONPATH`` fixture, this
    covers test bodies and function-scoped fixtures, not module or session
    fixtures, import-time ``PATH`` captures, or a test's own ``monkeypatch.undo()``.
    """
    monkeypatch.setenv(
        "PATH", interpreter_first_path(_python_shim_entry, os.environ.get("PATH", os.defpath))
    )


@pytest.fixture
def monkeypatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Undo every patch before ``tmp_path`` is torn down.

    ``tmp_path`` is requested first, so it is finalized last: patches of
    ``os.scandir``, ``os.walk`` or ``os.close`` are gone before the lock walk
    in the ``tmp_path`` override below and before pytest deletes a passing
    test's directory under ``tmp_path_retention_policy = "failed"``.
    """
    return monkeypatch


@pytest.fixture
def isolated_git_attributes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Attribute boundary tests select their own policy instead of the host's."""
    monkeypatch.setenv("GIT_ATTR_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "isolated-git.config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "isolated-xdg"))


@pytest.fixture
def isolated_git_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_git_attributes: None
) -> Path:
    """Return an empty excludes file without inherited command-scope Git policy.

    The suite's auto-maintenance settings are the only command-scope entries kept.
    """
    for key in tuple(os.environ):
        if key.startswith("GIT_CONFIG_") and key not in {
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
        }:
            monkeypatch.delenv(key)
    reset_git_command_config(monkeypatch)
    empty = tmp_path / "empty-excludes"
    empty.write_bytes(b"")
    return empty


def symlink_or_skip(link: Path, target: Path | str, **kwargs: object) -> None:
    """``link.symlink_to(target)``, skipping the test if the OS forbids it.

    Real symlink creation needs a privilege a stock Windows host lacks without
    Developer Mode / an elevated shell (``[WinError 1314] A required privilege
    is not held``). These code paths genuinely require symlinks, so skipping is
    honest — and lossless where they run in-container (Linux) in production.

    A link named ``.booley`` is also pinned, modelling a host that ran ``booley init``;
    tests of the pin itself (unpinned, retargeted, replaced) build the link with
    ``Path.symlink_to`` so this helper cannot mask the refusal they exercise.
    """
    try:
        link.symlink_to(target, **kwargs)  # type: ignore[arg-type]
    except OSError as exc:  # WinError 1314: needs Developer Mode / admin
        pytest.skip(f"symlink creation unavailable on this host: {exc}")
    if link.name == ".booley":
        pin_linked_booley_dir(link.parent)


def pin_linked_booley_dir(project: Path) -> None:
    """Record the host pin ``booley init`` writes for a linked ``<project>/.booley``."""
    from booley.core import booley_dir_pin

    booley_dir_pin.write_pin(project, booley_dir_pin.current_target(project / ".booley"))


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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--test-profile",
        choices=("python", "full"),
        default=None,
        help="Source tests: python excludes native B-Wave; full preflights it",
    )


def _validate_full_selection(config: pytest.Config) -> None:
    """A full profile must collect the complete source suite."""
    options = config.option
    filtered = any(
        (
            options.markexpr,
            options.keyword,
            options.deselect,
            options.ignore,
            options.ignore_glob,
            options.lf,
            options.stepwise,
            options.stepwise_skip,
            options.stepwise_reset,
        )
    )
    paths = [Path(arg).resolve() for arg in config.args]
    if filtered or paths != [(_SOURCE_ROOT / "tests").resolve()]:
        raise pytest.UsageError(
            "full profile requires the complete tests/ suite without filters; use unprofiled pytest for diagnostics"
        )


def _configure_test_profile(config: pytest.Config) -> None:
    profile = config.getoption("--test-profile")
    if profile == "python":
        expression = config.option.markexpr
        config.option.markexpr = (
            f"({expression}) and not native_bwave" if expression else "not native_bwave"
        )
    if profile == "full" and not hasattr(config, "workerinput") and not config.option.help:
        _validate_full_selection(config)
        from booley.dev_support.test_profiles import BWAVE_BUILD, native_test_problem

        problem = native_test_problem(_SOURCE_ROOT)
        if problem:
            raise pytest.UsageError(f"{problem}; run {' '.join(BWAVE_BUILD)}")


@pytest.fixture(autouse=True)
def _native_source_test_binary(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("native_bwave"):
        from booley.dev_support.test_profiles import native_test_binary

        binary = native_test_binary(_SOURCE_ROOT)
        monkeypatch.setenv("BOOLEY_BWAVE_BIN", str(binary))
        monkeypatch.setattr(
            "booley.runtime.paths.native_bwave_binary",
            lambda: binary if binary.is_file() else None,
        )
        from tests.bwave.native_binary import require_exact_binary

        monkeypatch.setattr(
            "booley.bwave.cli.native_bwave_binary", lambda: require_exact_binary(binary)
        )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if (
        item.config.getoption("--test-profile") == "full"
        and item.get_closest_marker("native_bwave")
        and report.skipped
        and not hasattr(report, "wasxfail")
        and not (os.name != "posix" and "FIFO requires POSIX" in str(report.longrepr))
    ):
        report.outcome = "failed"
        report.longrepr = f"Full profile requires native test execution: {report.longrepr}"
    return report


def hide_mutmut_stats_mode_from_children() -> None:
    """Remove mutmut's ``stats`` marker from the environment child processes inherit.

    Why: while collecting stats, mutmut calls ``set_mutant_under_test("stats")``,
    which also exports ``MUTANT_UNDER_TEST=stats``. A child process that a test
    spawns or execs (multiprocessing ``spawn``, a ``booley`` or pytest subprocess
    with the mutated source on ``PYTHONPATH``) inherits it, so its first call
    into mutated code tries to record a stats hit and loads mutmut's
    configuration. Tests often start such children from an empty temporary
    working directory (the harness autouse fixture), where mutmut finds no
    ``setup.cfg`` and the child dies with ``FileNotFoundError``.

    Relies on (mutmut >= 3.8.1, upstream mutmut #511): ``set_mutant_under_test``
    also keeps a process-local copy, and ``get_mutant_under_test`` falls back to
    it when the environment variable is missing. So the parent pytest process still records
    which tests cover which functions, while a fresh child sees no mode at all and
    runs the original, unmutated functions. Its hits were never recorded anyway,
    so no stats are lost. A forked child keeps the in-memory copy and mutmut's
    already-cached configuration, exactly as before. If a later mutmut drops the
    fallback, the parent stops recording and the campaign ends early with "could
    not find any test case for any mutant".

    Only ``stats`` is removed: a mutant run must keep exporting the mutant name so
    children exercise the mutant, and a plain pytest run has nothing to remove.
    """
    if os.environ.get("MUTANT_UNDER_TEST") == "stats":
        del os.environ["MUTANT_UNDER_TEST"]


def pytest_configure(config: pytest.Config) -> None:
    """Bound every test's wall-clock so a hang fails loudly instead of wedging
    the whole suite.

    Requires the ``pytest-timeout`` dev dependency. When that plugin is absent
    this is a deliberate no-op that emits *no* config warning, keeping the suite
    warning-free (principle 12) in bare environments.

    Runs for every session mutmut starts (it calls ``pytest.main`` repeatedly in
    one process), so the stats marker is hidden before any test starts a child.

    Also exports the session's Git auto-maintenance settings to every child.
    """
    hide_mutmut_stats_mode_from_children()
    _disable_git_auto_maintenance_for_children()
    _configure_test_profile(config)
    _begin_coverage_stamp(config)
    prune_orphan_basetemps(config)
    # Imported here: pytest_plugins must load it first for assertion rewriting.
    from tests.timeout_headroom import timeout_plugin_loaded

    _isolate_xdist_worker_temp()
    if timeout_plugin_loaded(config):
        from pytest_timeout import get_env_settings

        if get_env_settings(config).timeout is None:
            config.option.timeout = _DEFAULT_TEST_TIMEOUT_S


def prune_orphan_basetemps(config: pytest.Config) -> None:
    """Record this checkout's tools-cache basetemp and prune those of removed checkouts.

    Controller only: xdist workers share the controller's basetemp, and one
    prune per session is enough. See ``booley.dev_support.pytest_basetemp``.
    """
    if hasattr(config, "workerinput"):
        return
    from booley.dev_support.pytest_basetemp import record_and_prune

    record_and_prune(config.option.basetemp, _SOURCE_ROOT)


# (report, fingerprint) of a controller run that writes Cobertura XML; see
# booley.dev_support.changed_line_coverage.
_COVERAGE_STAMP = pytest.StashKey[tuple[Path, str]]()
_SESSION_PASSED = pytest.StashKey[bool]()


def _begin_coverage_stamp(config: pytest.Config) -> None:
    """Invalidate a pytest-cov XML report's stamp before this run rewrites it."""
    reports = getattr(config.option, "cov_report", None)  # absent without pytest-cov
    xml = reports.get("xml") if isinstance(reports, dict) else None
    if not xml or hasattr(config, "workerinput"):
        return
    from booley.dev_support.changed_line_coverage import begin_stamp

    report = Path(xml).resolve()
    config.stash[_COVERAGE_STAMP] = (report, begin_stamp(_SOURCE_ROOT, report))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remember whether the run passed; pytest-cov writes its XML after this."""
    session.config.stash[_SESSION_PASSED] = exitstatus == pytest.ExitCode.OK


def pytest_unconfigure(config: pytest.Config) -> None:
    """Stamp a green run's coverage report, then remove the worker-only temp root."""
    global _XDIST_WORKER_TEMP

    stamp = config.stash.get(_COVERAGE_STAMP, None)
    if stamp is not None and config.stash.get(_SESSION_PASSED, False):
        from booley.dev_support.changed_line_coverage import finish_stamp

        finish_stamp(_SOURCE_ROOT, *stamp)

    if _XDIST_WORKER_TEMP is not None:
        shutil.rmtree(_XDIST_WORKER_TEMP)
        _XDIST_WORKER_TEMP = None
        tempfile.tempdir = None


_FLOW_PUBLICATIONS = pytest.StashKey[list[tuple[str, Any]]]()


@pytest.fixture(autouse=True)
def _track_flow_invocations(request: pytest.FixtureRequest) -> Iterator[None]:
    """Record each Flow invocation's publication stack for the teardown lock check below.

    Minimal CI environments (for example the production-image smoke jobs) lack
    Flow dependencies such as PyYAML. No Flow invocation can exist there, so the
    fixture tracks nothing instead of failing every test's setup.
    """
    try:
        from booley.flows.base import BuiltinFlow
    except ModuleNotFoundError:
        yield
        return

    stacks = request.node.stash.setdefault(_FLOW_PUBLICATIONS, [])
    start = getattr(BuiltinFlow, "_start_invocation", None)
    if start is None:  # a Flow without an invocation-start hook has no stack to track
        yield
        return

    def tracking_start(flow: BuiltinFlow, *args: Any, **kwargs: Any) -> None:
        start(flow, *args, **kwargs)
        stacks.append((flow.name, flow.publication_resources))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(BuiltinFlow, "_start_invocation", tracking_start)
        yield


# Open (path, read handle) pairs for the invocation locks found under tmp_path.
_INVOCATION_LOCKS = pytest.StashKey[list[tuple[Path, BinaryIO]]]()


@pytest.fixture
def tmp_path(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    """Hold read handles on invocation locks before pytest deletes a passing test's tmp_path.

    ``tmp_path_retention_policy = "failed"`` removes the directory of a passing
    test in pytest's own ``tmp_path`` finalizer, before the teardown guard below
    walks it.  This fixture tears down first, so the guard can still probe locks
    a passing test leaked.  It only sees locks created while the test ran under
    the directory this fixture yields.
    """
    yield tmp_path
    if sys.platform != "win32":
        from tests.file_lock_probe import open_invocation_locks

        request.node.stash[_INVOCATION_LOCKS] = open_invocation_locks(tmp_path)


def _publication_leaks(
    stacks: list[tuple[str, Any]], locks: list[tuple[Path, BinaryIO]]
) -> list[str]:
    """Observe callbacks and locks before emergency cleanup can hide a leak."""
    # ExitStack has no public callback-count API; this is test-only introspection.
    leaks = [
        f"Flow invocation {name!r} ({id(stack):#x}) has publication callbacks"
        for name, stack in stacks
        if stack._exit_callbacks
    ]
    if locks:
        from tests.file_lock_probe import handle_lock_is_held

        leaks.extend(
            f"Simulation invocation lock still held: {path}"
            for path, handle in locks
            if handle_lock_is_held(handle)
        )
    return leaks


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Iterator[None]:
    """Detect leaked publication resources, then close every tracked stack.

    Direct Flow calls must own their publication scope. Probe the held lock
    handles after all fixture finalizers so test patches of Path or fcntl are
    no longer active.
    Emergency cleanup still runs when a finalizer or the leak probe fails.
    """
    try:
        return (yield)
    finally:
        stacks = item.stash.get(_FLOW_PUBLICATIONS, [])
        locks = item.stash.get(_INVOCATION_LOCKS, [])
        try:
            leaks = _publication_leaks(stacks, locks)
        finally:
            with ExitStack() as cleanup:
                for _name, handle in locks:
                    cleanup.callback(handle.close)
                for _name, stack in stacks:
                    cleanup.callback(stack.close)
        if leaks:
            pytest.fail("Publication resources leaked past the test:\n" + "\n".join(leaks))


@pytest.fixture(autouse=True)
def _isolate_host_lifecycle_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host-global mutation locks local to each test process."""
    from booley.runtime import lifecycle_lock

    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path / "host-config")


@pytest.fixture(autouse=True)
def _isolate_directory_layout_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-Git layout gates must not create coordination state in the real host config."""
    from booley.core import booley_layout

    monkeypatch.setattr(booley_layout, "config_dir", lambda: tmp_path / "layout-config" / "booley")


@pytest.fixture(autouse=True)
def _isolate_booley_dir_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linked-entry pins must not be written to the real host config."""
    from booley.core import booley_dir_pin

    monkeypatch.setattr(booley_dir_pin, "config_dir", lambda: tmp_path / "pin-config" / "booley")


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


def write_activity_signal(booley_dir: Path, key: str, state: str, at: str, tool: str = "") -> Path:
    """Write one session-activity signal in the hook script's on-disk format."""
    import json

    from booley.runtime.session_activity import SCHEMA, ActivityStore

    path = ActivityStore(booley_dir).path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    signal = {"schema": SCHEMA, "key": key, "state": state, "at": at}
    path.write_text(json.dumps(signal | ({"tool": tool} if tool else {})), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _inline_ended_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep cleanup deterministic; worker tests explicitly restore the process launcher."""
    from booley.goals import ended_records
    from booley.goals.store import GoalStore

    monkeypatch.setattr(
        ended_records,
        "_launch_cleanup",
        lambda booley_dir, repo_dir: ended_records.sweep_ended_records(
            GoalStore(booley_dir), repo_dir
        ),
    )


_FAKE_BWAVE = '''#!{python}
"""Stand-in for ``bwave build``: modes come from FAKE_BWAVE_MODE."""
import os
import sys
import time

args = sys.argv[1:]
store = args[args.index("-o") + 1]
mode = os.environ.get("FAKE_BWAVE_MODE", "ok")
if mode == "never_read":
    time.sleep(60)
if mode == "exit_early":
    sys.stderr.write("fake bwave refused the input\\n")
    sys.exit(1)
data = sys.stdin.buffer.read()
if b"$enddefinitions" not in data:
    sys.stderr.write("fake bwave: no VCD header\\n")
    sys.exit(1)
with open(store, "wb") as out:
    out.write({store_bytes!r})
'''


@pytest.fixture
def fake_bwave(tmp_path, monkeypatch):
    """A POSIX ``bwave build`` fake selected as the native binary (never via PATH).

    It reads stdin to EOF and writes a minimal store when it saw a VCD header,
    else exits 1. ``FAKE_BWAVE_MODE`` = ``never_read`` (sleeps without reading
    stdin) or ``exit_early`` (exits 1 at once).
    """
    from booley.bwave import waveform_store

    script = tmp_path / "fake-bin" / "bwave"
    script.parent.mkdir()
    script.write_text(
        _FAKE_BWAVE.format(python=sys.executable, store_bytes=MINIMAL_FST_BYTES), encoding="utf-8"
    )
    script.chmod(0o755)
    monkeypatch.setattr(waveform_store, "native_bwave_binary", lambda: str(script))
    return script
