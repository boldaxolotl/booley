"""Persist timeout evidence before pytest-timeout terminates a worker."""

from __future__ import annotations

import faulthandler
import json
import os
import threading
from collections.abc import Generator
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

import pytest


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
    # execnet sends worker stdout to the null device. Write before the plugin's
    # os._exit(1), which cannot deliver a normal captured pytest failure report.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
        with (directory / f"timeout-{worker}-{os.getpid()}.log").open(
            "w", encoding="utf-8"
        ) as report:
            report.write(
                json.dumps(
                    {
                        "nodeid": item.nodeid,
                        "timeout_seconds": settings.timeout,
                        "worker": worker,
                        "pid": os.getpid(),
                    }
                )
                + "\n"
            )
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
    import subprocess

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
