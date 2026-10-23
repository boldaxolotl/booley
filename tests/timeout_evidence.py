"""Persist timeout evidence before pytest-timeout terminates a worker.

When ``BOOLEY_PYTEST_EVIDENCE_DIR`` is set, a thread timeout writes
``timeout-<worker>-<pid>.log``: one JSON header line, then the faulthandler
stack dump. Header keys: ``nodeid``, ``timeout_seconds``, ``worker``, ``pid``;
``timed_out_at`` (UTC, RFC 3339, second resolution); ``children`` (one entry
per unreaped ``subprocess.Popen`` of this process: ``pid``, ``args`` as a
string or a list of strings, ``started_at``, ``elapsed_seconds``, ``running``,
``returncode``, and ``reaped_elsewhere`` only when true; ``pid`` and ``error``
alone when that one child could not be described); ``children_error``
instead of ``children`` when collection failed. Children are tracked through
weak references, so on Windows a ``Popen`` dropped by its owner while its
child still runs is not reported (POSIX also scans ``subprocess._active``).
"""

from __future__ import annotations

import faulthandler
import functools
import json
import os
import subprocess
import threading
import time
import weakref
from collections.abc import Generator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import pytest

# Weak references: a strong registry would keep each Popen's pipes open and
# could stop a child from ever seeing stdin EOF.
_CHILDREN: weakref.WeakSet[subprocess.Popen[Any]] = weakref.WeakSet()
_CHILDREN_LOCK = threading.Lock()
_LOCK_WAIT_SECONDS = 1.0


def _utc_now() -> str:
    """Return the current UTC time as RFC 3339 with second resolution."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _install_child_registry() -> None:
    """Record every Popen this process creates (test infrastructure, not production)."""
    original = subprocess.Popen.__init__
    if getattr(original, "_booley_child_registry", False):
        return

    @functools.wraps(original)
    def register(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self._booley_started = (_utc_now(), time.monotonic())  # type: ignore[attr-defined]
        with _CHILDREN_LOCK:
            _CHILDREN.add(self)

    register._booley_child_registry = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = register  # type: ignore[method-assign]


def pytest_configure(config: pytest.Config) -> None:
    """Start tracking child processes when timeout evidence is requested."""
    if os.environ.get("BOOLEY_PYTEST_EVIDENCE_DIR"):
        _install_child_registry()


def _child_state(popen: subprocess.Popen[Any]) -> dict[str, Any]:
    """Return running/returncode without reaping the child (Windows ``poll`` may set returncode)."""
    if os.name == "nt":
        returncode = popen.poll()
        return {"running": returncode is None, "returncode": returncode}
    try:
        exited = os.waitid(os.P_PID, popen.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        # Reaped by another waiter; Popen.poll() would misreport returncode 0.
        return {"running": False, "returncode": None, "reaped_elsewhere": True}
    if exited is None:
        return {"running": True, "returncode": None}
    code = exited.si_status if exited.si_code == os.CLD_EXITED else -exited.si_status
    return {"running": False, "returncode": code}


def _command_args(popen: subprocess.Popen[Any]) -> str | list[str]:
    """Return Popen.args as a string or a list of strings."""
    args = popen.args
    if isinstance(args, str | bytes | os.PathLike):
        return os.fsdecode(args)
    return [os.fsdecode(arg) if isinstance(arg, bytes | os.PathLike) else str(arg) for arg in args]


def _child_evidence() -> list[dict[str, Any]]:
    """Describe each unreaped child without blocking on it."""
    if not _CHILDREN_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise TimeoutError("child registry lock is held")
    try:
        popens = list(_CHILDREN)
    finally:
        _CHILDREN_LOCK.release()
    if os.name != "nt":
        # Popens dropped while their child still runs live here, not in the registry.
        popens += [popen for popen in subprocess._active or () if popen not in popens]  # type: ignore[attr-defined]
    entries = []
    for popen in popens:
        started = getattr(popen, "_booley_started", None)
        if popen.returncode is not None or started is None:
            continue
        started_at, started_monotonic = started
        try:
            entry = {
                "pid": popen.pid,
                "args": _command_args(popen),
                "started_at": started_at,
                "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                **_child_state(popen),
            }
        except Exception as error:  # noqa: BLE001 - one bad child must not hide the others
            entry = {"pid": popen.pid, "error": f"{type(error).__name__}: {error}"}
        entries.append(entry)
    return entries


@pytest.hookimpl(optionalhook=True, tryfirst=True)
def pytest_timeout_set_timer(item: pytest.Item, settings: Any) -> bool | None:
    """Use the plugin's thread timeout with a durable diagnostic preamble."""
    directory = os.environ.get("BOOLEY_PYTEST_EVIDENCE_DIR")
    thread_method = settings.method == "thread" or (
        settings.method == "signal" and threading.current_thread() is not threading.main_thread()
    )
    if not directory or not thread_method:
        return None

    timer = threading.Timer(settings.timeout, _thread_timeout, (item, settings, Path(directory)))
    timer.name = f"pytest-timeout {item.nodeid}"

    def cancel() -> None:
        timer.cancel()
        timer.join()

    item.cancel_timeout = cancel
    timer.start()
    return True


def _thread_timeout(item: pytest.Item, settings: Any, directory: Path) -> None:
    from pytest_timeout import is_debugging, timeout_timer

    if not settings.disable_debugger_detection and is_debugging():
        return
    timed_out_at = _utc_now()
    # execnet sends worker stdout to the null device. Write before the plugin's
    # os._exit(1), which cannot deliver a normal captured pytest failure report.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
        header: dict[str, Any] = {
            "nodeid": item.nodeid,
            "timeout_seconds": settings.timeout,
            "worker": worker,
            "pid": os.getpid(),
            "timed_out_at": timed_out_at,
        }
        try:
            header["children"] = _child_evidence()
            json.dumps(header["children"])
        except Exception as error:  # noqa: BLE001 - evidence must never block the kill
            header.pop("children", None)
            header["children_error"] = f"{type(error).__name__}: {error}"
        with (directory / f"timeout-{worker}-{os.getpid()}.log").open(
            "w", encoding="utf-8"
        ) as report:
            report.write(json.dumps(header) + "\n")
            report.flush()
            faulthandler.dump_traceback(file=report, all_threads=True)
    finally:
        # A failed evidence write must never disable the bounded timeout.
        timeout_timer(item, settings)


_FATAL_REPORT = pytest.StashKey[TextIO]()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Route native fatal-error stacks to a retained file instead of worker stderr."""
    directory = os.environ.get("BOOLEY_PYTEST_EVIDENCE_DIR")
    if not directory or not session.config.pluginmanager.hasplugin("faulthandler"):
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    report = (root / f"fatal-{worker}-{os.getpid()}.log").open("w", encoding="utf-8")
    session.config.stash[_FATAL_REPORT] = report
    faulthandler.enable(file=report, all_threads=True)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_unconfigure(config: pytest.Config) -> Generator[None, object, object]:
    """Retain fatal-error stacks through pytest shutdown and restore its handler safely."""
    try:
        return (yield)
    finally:
        report = config.stash.get(_FATAL_REPORT, None)
        if report is not None:
            # pytest's faulthandler plugin has now disabled this descriptor
            # and restored the original handler, if one was enabled.
            report.close()
            path = Path(report.name)
            if path.stat().st_size == 0:
                path.unlink()


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object | None) -> None:
    """Record a failed local worker's process exit code at the controller."""
    directory = os.environ.get("BOOLEY_PYTEST_EVIDENCE_DIR")
    if not directory or error is None:
        return
    # xdist/execnet have no public process-exit-code API. The pinned local popen
    # transport owns this Popen; remote transports are recorded as unknown.
    process = getattr(getattr(node.gateway, "_io", None), "popen", None)
    exit_code = None
    if process is not None:
        with suppress(subprocess.TimeoutExpired):
            exit_code = process.wait(timeout=5)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    metadata = {"worker": node.gateway.id, "exit_code": exit_code, "error": str(error)}
    (root / f"worker-exit-{node.gateway.id}.json").write_text(
        json.dumps(metadata) + "\n", encoding="utf-8"
    )
    reporter = node.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"Worker {node.gateway.id} process exit code: {exit_code}")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Close each timing record before the next test can terminate the process.

    Configure one file per pytest controller. Under xdist only the controller
    writes reports forwarded by workers, avoiding duplicate concurrent writes.
    """
    destination = os.environ.get("BOOLEY_PYTEST_TIMING_FILE")
    if not destination or "PYTEST_XDIST_WORKER" in os.environ:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "nodeid": report.nodeid,
        "phase": report.when,
        "outcome": report.outcome,
        "duration_seconds": report.duration,
        "recorded_at": _utc_now(),
    }
    if report.failed:
        payload["longrepr"] = report.longreprtext
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload) + "\n")
