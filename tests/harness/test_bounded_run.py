"""The bounded subprocess runner behind the Node suite tests reports a timeout usefully.

Issue #2679: ``subprocess.run(timeout=...)`` raised a ``TimeoutExpired`` whose
text omitted everything the command had printed, so a stalled Windows CI run
left no clue where it stopped.
"""

from __future__ import annotations

import sys
import time

import pytest

from tests.harness import test_dashboard_extension as node_suite


def _stub_command(code: str) -> list[str]:
    """Return a command that runs the Python snippet ``code`` in a fresh interpreter."""
    return [sys.executable, "-c", code]


@pytest.mark.timeout(90)
def test_bounded_run_timeout_reports_partial_output(tmp_path):
    stub = _stub_command(
        "import sys, time\n"
        "print('STDOUT-MARKER', flush=True)\n"
        "print('STDERR-MARKER', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        node_suite.run_bounded(stub, bound_s=1, output_dir=tmp_path)
    message = str(excinfo.value)
    assert "STDOUT-MARKER" in message
    assert "STDERR-MARKER" in message
    assert "1 s" in message


@pytest.mark.timeout(90)
def test_bounded_run_timeout_says_when_nothing_was_written(tmp_path):
    with pytest.raises(pytest.fail.Exception) as excinfo:
        node_suite.run_bounded(
            _stub_command("import time; time.sleep(60)"), bound_s=1, output_dir=tmp_path
        )
    assert str(excinfo.value).count("(nothing)") == 2


@pytest.mark.timeout(90)
def test_bounded_run_timeout_kills_the_process_tree(tmp_path):
    # The stub and its grandchild both leave a sentinel file if they outlive the bound.
    grandchild = (
        "import pathlib, time; time.sleep(3); "
        f"pathlib.Path({str(tmp_path / 'survived-grandchild')!r}).write_text('x')"
    )
    stub = _stub_command(
        "import subprocess, sys, time, pathlib\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(3)\n"
        f"pathlib.Path({str(tmp_path / 'survived-stub')!r}).write_text('x')\n"
    )
    launched = time.monotonic()
    with pytest.raises(pytest.fail.Exception):
        node_suite.run_bounded(stub, bound_s=1, output_dir=tmp_path)
    time.sleep(max(0.0, 5 - (time.monotonic() - launched)))
    assert not (tmp_path / "survived-stub").exists()
    assert not (tmp_path / "survived-grandchild").exists()


def test_bounded_run_returns_output_of_a_nonzero_exit(tmp_path):
    stub = _stub_command(
        "import sys; print('some out'); print('some err', file=sys.stderr); sys.exit(3)"
    )
    code, out, err = node_suite.run_bounded(stub, bound_s=30, output_dir=tmp_path)
    assert code == 3
    assert "some out" in out
    assert "some err" in err
