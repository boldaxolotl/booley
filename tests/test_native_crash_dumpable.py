"""Guard the Linux crash-report suppression of the deliberate native-crash tests."""

from __future__ import annotations

import sys

import pytest

from tests.subprocess_budget import run_bounded_child
from tests.test_timeout_evidence import _NATIVE_CRASH  # pyright: ignore[reportPrivateUsage]


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="PR_SET_DUMPABLE is a Linux process flag"
)
def test_native_crash_marks_the_linux_child_non_dumpable() -> None:
    """The crash snippet must clear the dumpable flag before it crashes.

    ``RLIMIT_CORE=0`` does not stop a piped ``core_pattern`` handler such as
    Apport; only a non-dumpable process skips the dump before the helper runs.
    The probe swaps the crash for a print of ``PR_GET_DUMPABLE`` (3), so nothing
    crashes and the check is the same on every Linux host.
    """
    probe = (
        "import ctypes, faulthandler; "
        "faulthandler._read_null = lambda: print("
        "ctypes.CDLL(None).prctl(3, 0, 0, 0, 0), end='')"
    )
    result = run_bounded_child([sys.executable, "-c", probe + "; " + _NATIVE_CRASH])

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0"
