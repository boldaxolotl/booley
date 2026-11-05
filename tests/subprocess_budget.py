"""Shared time budget for subprocesses that test helpers launch.

CI runs every test under ``--timeout=60`` (``.github/workflows/test.yml``) and
the Windows lanes add ``--timeout-headroom=0.8`` (48 s of useful work).  A
helper that waits on a child process must therefore give up well inside that
budget, so a hung child fails one test with a message naming the command
instead of freezing a pytest-xdist worker until the whole job is killed.
"""

from __future__ import annotations

#: Seconds a test helper may wait for a script or external tool (git, bash, ...).
#: Half of the 60 s per-test budget and under the 48 s Windows headroom limit.
SUBPROCESS_TIMEOUT_S = 30
