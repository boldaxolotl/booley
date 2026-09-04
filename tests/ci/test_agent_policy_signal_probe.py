from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "tests/docker/agent_policy_probe.py"
SPEC = importlib.util.spec_from_file_location("agent_policy_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
agent_policy_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_policy_probe)


def test_signal_probe_rejects_server_without_expected_child() -> None:
    command = [
        sys.executable,
        "-c",
        "import signal, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(143)); "
        "time.sleep(30)",
    ]

    with pytest.raises(AssertionError, match="started no descendant process"):
        agent_policy_probe._probe_signal(
            "childless",
            command,
            os.environ.copy(),
            readiness_timeout=0.2,
        )


def test_signal_probe_observes_child_and_waits_for_group_cleanup(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(
        "import signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "def stop(*_args):\n"
        "    child.terminate()\n"
        "    child.wait(timeout=5)\n"
        "    raise SystemExit(143)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )

    exit_code = agent_policy_probe._probe_signal(
        "server",
        [sys.executable, str(server)],
        os.environ.copy(),
        readiness_timeout=2,
    )

    assert exit_code == 143
