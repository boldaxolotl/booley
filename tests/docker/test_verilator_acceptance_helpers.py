"""Regression for the acceptance gate's abnormal termination cleanup."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys

import pytest
from tests.docker.verilator_acceptance import terminate_and_reap


@pytest.mark.skipif(os.name == "nt", reason="the image acceptance gate uses POSIX signals")
def test_ignored_sigterm_is_killed_and_timeout_remains_a_failure() -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 5)[0], "child did not become ready"
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(subprocess.TimeoutExpired):
            terminate_and_reap(child, timeout=0.05)
        assert child.poll() == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
