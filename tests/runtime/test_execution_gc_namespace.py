"""Execution GC must run beside Sandbox identities, not in the host PID namespace."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.execution_test_support import runtime_env, wait_for
from tests.runtime.scratch_support import scratch_process_ownership_supported

from booley.core.temporary_workspace import process_identity
from booley.runtime.execution_records import (
    execution_paths,
    gc_terminal_executions,
    write_attachment_heartbeat,
)

pytestmark = pytest.mark.skipif(
    not scratch_process_ownership_supported(), reason="Linux scratch identity"
)

_CONTROLLER = """
import json, os, subprocess, sys, time
from pathlib import Path
from booley.core.temporary_workspace import process_identity
from booley.runtime.execution_records import execution_paths, write_attachment_heartbeat
state = Path(sys.argv[1])
def run(value):
    paths = execution_paths(value * 32, booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    result = subprocess.run([
        sys.executable, '-m', 'booley.runtime.execution_supervisor', 'run',
        '--execution-id', value * 32, '--attachment-timeout-seconds', '60', '--', 'true'
    ], check=False, timeout=15)
    assert result.returncode == 0
    return paths
old = run('f')
(state / 'ready').write_text(json.dumps(process_identity(os.getpid())['namespace']))
deadline = time.monotonic() + 30
while not (state / 'host-finished').exists() and time.monotonic() < deadline:
    time.sleep(.01)
assert (state / 'host-finished').exists()
"""


def test_real_namespace_retirement_precedes_retention_and_namespace_exit(tmp_path: Path):
    executable = shutil.which("unshare")
    if executable is None:
        pytest.skip("unshare is unavailable")
    namespace = [
        executable,
        "--user",
        "--map-root-user",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
        "--mount-proc",
    ]
    probe = subprocess.run([*namespace, "true"], check=False, capture_output=True, timeout=10)
    if probe.returncode:
        pytest.skip("Kernel disallows unprivileged PID/mount namespaces")
    process = subprocess.Popen(
        [*namespace, sys.executable, "-c", _CONTROLLER, str(tmp_path)],
        env=runtime_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = tmp_path / "ready"
        wait_for(
            ready.exists, failure="PID-namespace supervisor did not become ready", timeout_s=15
        )
        assert json.loads(ready.read_text()) != process_identity(os.getpid())["namespace"]
        old = execution_paths("f" * 32, booley_dir=tmp_path)
        assert not list(old.root.glob(".booley-shutdown-*"))
        assert gc_terminal_executions(tmp_path) == []
        assert old.record.exists()
        (tmp_path / "host-finished").touch()
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    os.utime(old.record, (1, 1))
    _collect_from_new_namespace(tmp_path)
    assert not old.root.exists()


def _collect_from_new_namespace(state: Path) -> None:
    paths = execution_paths("e" * 32, booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            paths.root.name,
            "--attachment-timeout-seconds",
            "60",
            "--",
            "true",
        ],
        env=runtime_env(state),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
