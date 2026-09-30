"""Exercise the suite guard in child pytest runs, including expected failures."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


_CHILD_TESTS = """
from pathlib import Path
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.campaign_reports import campaign_invocation_lock
from tests.file_lock_probe import lock_is_held

marker = Path(__MARKER__)
reservation = None
resources = None

def test_leak(tmp_path):
    global reservation, resources
    flow = SimulateFlow()
    resources = flow.context.publication_resources
    if __LEAK__ == "reservation":
        reservation = flow._next_invocation_dir(tmp_path / "reports")
        resources.callback(marker.touch)
    elif __LEAK__ == "callback":
        resources.callback(marker.touch)
    elif __LEAK__ == "lock":
        tmp_path.mkdir(exist_ok=True)
        reservation = tmp_path / "1"
        campaign_invocation_lock_context = campaign_invocation_lock(reservation)
        campaign_invocation_lock_context.__enter__()
        # Retain it outside the Flow session so the independent probe is tested.
        resources = campaign_invocation_lock_context
    else:
        with resources:
            reservation = flow._next_invocation_dir(tmp_path / "reports")
            resources.callback(marker.touch)

def test_cleanup():
    if __LEAK__ == "lock":
        resources.__exit__(None, None, None)
    else:
        assert marker.exists()
        assert not resources._exit_callbacks
    if reservation is not None:
        lock = reservation.with_name(f".invocation-{reservation.name}.lock")
        assert not lock_is_held(lock)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="invocation lock probe uses fcntl")
@pytest.mark.parametrize("leak", ["reservation", "callback", "lock", "none"])
def test_publication_guard_detects_before_cleanup(tmp_path: Path, leak: str) -> None:
    """Leaks fail teardown, name their owner, and still release every resource."""
    (tmp_path / "conftest.py").write_text(
        (REPO_ROOT / "tests/conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    marker = tmp_path / "cleaned"
    (tmp_path / "test_child.py").write_text(
        _CHILD_TESTS.replace("__MARKER__", repr(str(marker))).replace("__LEAK__", repr(leak)),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT)))
    for name in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--confcutdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert "2 passed" in output, output
    assert result.returncode == (0 if leak == "none" else 1), output
    if leak in {"reservation", "callback"}:
        assert "Flow session 'sim'" in output, output
    if leak in {"reservation", "lock"}:
        assert "Simulation invocation lock still held:" in output, output
        assert ".invocation-1.lock" in output, output
