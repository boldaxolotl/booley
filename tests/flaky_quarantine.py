"""Quarantined flaky tests: they run, but failing again after the rerun only warns.

Each entry is ``(test id, owner issue number)``. The test id is the exact pytest
node ID, parameter case included; the owner issue is the open ``flaky`` issue
whose title names it. Rules and the bar for adding an entry:
``.github/ci/README.md#quarantine-of-flaky-tests``.
"""

QUARANTINE: tuple[tuple[str, int], ...] = ()
