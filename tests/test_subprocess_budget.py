"""``run_bounded_child`` fails a hung child usefully and keeps ``check=True`` semantics."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.subprocess_budget import run_bounded_child


@pytest.mark.timeout(90)
def test_run_bounded_child_timeout_notes_when_nothing_was_captured() -> None:
    with pytest.raises(pytest.fail.Exception) as raised:
        run_bounded_child([sys.executable, "-c", "import time; time.sleep(60)"], timeout=1)

    message = str(raised.value)
    assert "timed out after 1 s" in message
    assert message.count("(none captured)") == 2


@pytest.mark.timeout(90)
def test_run_bounded_child_timeout_keeps_partial_output() -> None:
    script = (
        "import sys, time\n"
        "print('OUT-PARTIAL', flush=True)\n"
        "print('ERR-PARTIAL', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(pytest.fail.Exception) as raised:
        run_bounded_child([sys.executable, "-c", script], timeout=10)

    message = str(raised.value)
    assert "OUT-PARTIAL" in message
    assert "ERR-PARTIAL" in message
    assert "(none captured)" not in message


def test_run_bounded_child_timeout_notes_pipes_abandoned_after_the_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def abandoned(argv: list[str], **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(argv, 5, output=None, stderr=None)

    monkeypatch.setattr("tests.subprocess_budget.run_captured_bounded", abandoned)
    with pytest.raises(pytest.fail.Exception) as raised:
        run_bounded_child(["tool"], timeout=5)

    assert str(raised.value).count("(not collected") == 2


def test_run_bounded_child_check_raises_called_process_error() -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_bounded_child([sys.executable, "-c", script], check=True)

    assert raised.value.returncode == 3
    assert raised.value.stdout.strip() == "out"
    assert raised.value.stderr.strip() == "err"


def test_run_bounded_child_returns_a_failed_child_without_check() -> None:
    result = run_bounded_child([sys.executable, "-c", "raise SystemExit(4)"])

    assert result.returncode == 4
