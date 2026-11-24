"""``run_bounded_child`` fails a hung child usefully and keeps ``check=True`` semantics."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.subprocess_budget import run_bounded_child
from tests.time_budget import scaled

_SHORT_TIMEOUT_S = scaled(1)


@pytest.mark.timeout(90)
@pytest.mark.parametrize(
    ("script", "captured"),
    [
        pytest.param("import time; time.sleep(60)", False, id="nothing-captured"),
        pytest.param(
            "import sys, time\n"
            "print('OUT-PARTIAL', flush=True)\n"
            "print('ERR-PARTIAL', file=sys.stderr, flush=True)\n"
            "time.sleep(60)\n",
            True,
            id="partial-output",
        ),
    ],
)
def test_run_bounded_child_timeout_reports_captured_output(script: str, captured: bool) -> None:
    timeout = scaled(10) if captured else _SHORT_TIMEOUT_S
    with pytest.raises(pytest.fail.Exception) as raised:
        run_bounded_child([sys.executable, "-c", script], timeout=timeout)

    message = str(raised.value)
    if captured:
        assert "OUT-PARTIAL" in message
        assert "ERR-PARTIAL" in message
        assert "(none captured)" not in message
    else:
        assert f"timed out after {timeout:g} s" in message
        assert message.count("(none captured)") == 2


def test_run_bounded_child_timeout_notes_pipes_abandoned_after_the_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def abandoned(argv: list[str], **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(argv, 5, output=None, stderr=None)

    monkeypatch.setattr("tests.subprocess_budget.run_captured_bounded", abandoned)
    with pytest.raises(pytest.fail.Exception) as raised:
        run_bounded_child(["tool"], timeout=5)

    assert str(raised.value).count("(not collected") == 2


def test_run_bounded_child_check_controls_failed_child_handling() -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    assert run_bounded_child([sys.executable, "-c", script]).returncode == 3

    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_bounded_child([sys.executable, "-c", script], check=True)

    assert raised.value.returncode == 3
    assert raised.value.stdout.strip() == "out"
    assert raised.value.stderr.strip() == "err"
