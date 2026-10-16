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
from booley.runtime.execution_records import execution_paths, gc_terminal_executions

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
os.utime(old.record, (1, 1))
(state / 'ready').write_text(json.dumps(process_identity(os.getpid())['namespace']))
deadline = time.monotonic() + 30
while not (state / 'host-finished').exists() and time.monotonic() < deadline:
    time.sleep(.01)
assert (state / 'host-finished').exists()
run('e')
assert not old.root.exists(), 'In-Sandbox supervisor did not collect its old execution'
"""


def test_real_pid_namespace_supervisor_collects_records_that_host_cannot_prove(tmp_path: Path):
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
        assert gc_terminal_executions(tmp_path) == []
        assert execution_paths("f" * 32, booley_dir=tmp_path).record.exists()
        (tmp_path / "host-finished").touch()
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
