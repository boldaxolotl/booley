"""Bounded readiness and reaping for synthetic scratch subprocess tests."""

from __future__ import annotations

import os
import subprocess

from tests.execution_test_support import wait_for


def wait(predicate, seconds=5):
    wait_for(predicate, failure="scratch child did not settle", timeout_s=seconds)


def reap_guardian(pid: int) -> None:
    wait(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid)


def stop_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is None:
        worker.kill()
    worker.wait(timeout=10)
