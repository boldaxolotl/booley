"""Persist timeout evidence before pytest-timeout terminates a worker."""

from __future__ import annotations

import faulthandler
import json
import os
import threading
from pathlib import Path
from typing import Any

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
