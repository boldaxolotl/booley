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
            if __LEAK__ == "none":
                reservation = flow._next_invocation_dir(tmp_path / "reports")
            resources.callback(marker.touch)

def test_cleanup():
    if __LEAK__ == "lock":
        resources.__exit__(None, None, None)
    else:
        assert marker.exists()
        assert not resources._exit_callbacks
    if reservation is not None:
        from tests.file_lock_probe import lock_is_held

        lock = reservation.with_name(f".invocation-{reservation.name}.lock")
        assert not lock_is_held(lock)
"""


_LOCK_PROBE_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="invocation lock probe uses fcntl"
)


@pytest.mark.parametrize(
    "leak",
    [
        pytest.param("reservation", marks=_LOCK_PROBE_ONLY),
        "callback",
        pytest.param("lock", marks=_LOCK_PROBE_ONLY),
        pytest.param("none", marks=_LOCK_PROBE_ONLY),
        "owned_callback",
    ],
)
def test_publication_guard_detects_before_cleanup(tmp_path: Path, leak: str) -> None:
    """Leaks fail teardown, name their owner, and still release every resource."""
    marker = tmp_path / "cleaned"
    source = _CHILD_TESTS.replace("__MARKER__", repr(str(marker))).replace("__LEAK__", repr(leak))
    result = _run_child_pytest(tmp_path, source)
    output = result.stdout + result.stderr
    assert "2 passed" in output, output
    assert result.returncode == (0 if leak in {"none", "owned_callback"} else 1), output
    if leak in {"reservation", "callback"}:
        assert "Flow session 'sim'" in output, output
    if leak in {"reservation", "lock"}:
        assert "Simulation invocation lock still held:" in output, output
        assert ".invocation-1.lock" in output, output


def _run_child_pytest(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """Run the real suite guard in an isolated, bounded child pytest process."""
    (tmp_path / "conftest.py").write_text(
        (REPO_ROOT / "tests/conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "test_child.py").write_text(source, encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT)))
    for name in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--confcutdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


_CHILD_FAILURE_TESTS = """
from pathlib import Path
import pytest
from booley.flows.sim.flow import SimulateFlow

root = Path(__ROOT__)
resources = []
guard = None
original_probe = None

def fail_probe(*args):
    raise RuntimeError("intentional probe failure")

def fail_finalizer():
    (root / "finalizer-ran").touch()
    pytest.fail("intentional finalizer failure")

def fail_cleanup():
    raise RuntimeError("intentional cleanup failure")

def test_failure(request):
    global guard, original_probe
    for index in range(2):
        session = SimulateFlow().context
        resources.append(session.publication_resources)
        session.publication_resources.callback((root / f"cleaned-{index}").touch)
    if __FAILURE__ == "cleanup":
        resources[-1].callback(fail_cleanup)
    elif __FAILURE__ == "finalizer":
        request.addfinalizer(fail_finalizer)
    else:
        guard = request.config.pluginmanager.getplugin(str(root / "conftest.py"))
        assert guard is not None
        original_probe = guard._publication_leaks
        # Install after test fixtures unwind so the real guard sees the failure.
        request.addfinalizer(lambda: setattr(guard, "_publication_leaks", fail_probe))

def test_cleanup():
    if guard is not None:
        guard._publication_leaks = original_probe
    if __FAILURE__ == "finalizer":
        assert (root / "finalizer-ran").exists()
    assert len(resources) == 2
    for index, resource in enumerate(resources):
        assert (root / f"cleaned-{index}").exists()
        assert not resource._exit_callbacks
"""


@pytest.mark.parametrize("failure", ["cleanup", "finalizer", "probe"])
def test_publication_guard_cleans_sessions_after_failure(tmp_path: Path, failure: str) -> None:
    """A failing callback, finalizer, or probe cannot strand another session."""
    source = _CHILD_FAILURE_TESTS.replace("__ROOT__", repr(str(tmp_path))).replace(
        "__FAILURE__", repr(failure)
    )
    result = _run_child_pytest(tmp_path, source)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "2 passed, 1 error" in output, output
    if failure == "finalizer":
        assert "Publication resources leaked past the test" in output, output
    else:
        assert f"intentional {failure} failure" in output, output
