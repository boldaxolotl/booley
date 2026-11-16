"""One scaled time-budget policy for test waits.

Two kinds of budget exist:

* a deadline a test asserts on (``_HOOK_DEADLINE_S = scaled(5)``): the test names
  its own base budget and ``scaled`` multiplies it by the factor;
* the hang guard (``hang_guard_s``): one generous budget for waits that only stop
  a hang and assert nothing about time.

``BOOLEY_TEST_TIME_SCALE`` is the factor; unset means 1. Windows CI sets it to 2.
The policy and the per-test limit it implies are in
``.github/ci/README.md#test-time-budgets``.
"""

from __future__ import annotations

import math
import os

SCALE_VARIABLE = "BOOLEY_TEST_TIME_SCALE"
HANG_GUARD_BASE_S = 30


class TimeScaleError(ValueError):
    """Raised when ``BOOLEY_TEST_TIME_SCALE`` is not a positive finite number."""


def time_scale() -> float:
    """Return the factor in ``BOOLEY_TEST_TIME_SCALE``, or 1.0 when it is unset."""
    raw = os.environ.get(SCALE_VARIABLE)
    if raw is None:
        return 1.0
    try:
        factor = float(raw)
    except ValueError:
        factor = math.nan
    if not math.isfinite(factor) or factor <= 0:
        raise TimeScaleError(
            f"{SCALE_VARIABLE}={raw!r} is not a positive finite number; "
            "unset it for 1 or set a factor such as 2"
        )
    return factor


def scaled(base_s: float) -> float:
    """Return *base_s* seconds multiplied by the time scale."""
    return base_s * time_scale()


def hang_guard_s() -> int:
    """Return the whole-second budget for a wait that only stops a hang."""
    return math.ceil(scaled(HANG_GUARD_BASE_S))
