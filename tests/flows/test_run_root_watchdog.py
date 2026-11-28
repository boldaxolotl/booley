"""The removed-root watchdog's bookkeeping of the tools it signals (#5372)."""

from __future__ import annotations

from booley.flows.run_root_watchdog import _signal_each


def test_a_gone_target_is_forgotten_so_a_reused_number_is_left_alone() -> None:
    targets, sent = {1, 2}, []

    def send(target: int) -> None:
        sent.append(target)
        if target == 1:
            raise ProcessLookupError

    _signal_each(targets, send)
    _signal_each(targets, send)

    assert targets == {2}
    assert sorted(sent) == [1, 2, 2]
