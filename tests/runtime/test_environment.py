"""Scoped process-environment contracts."""

from __future__ import annotations

import asyncio
import os

import pytest

from booley.runtime.environment import scoped_environment


def test_scoped_environment_restores_all_keys_after_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOOLEY_EXISTING", "before")
    monkeypatch.delenv("BOOLEY_ADDED", raising=False)

    with (
        pytest.raises(RuntimeError, match="stop"),
        scoped_environment(
            {
                "BOOLEY_EXISTING": "during",
                "BOOLEY_ADDED": "temporary",
            }
        ),
    ):
        assert os.environ["BOOLEY_EXISTING"] == "during"
        assert os.environ["BOOLEY_ADDED"] == "temporary"
        raise RuntimeError("stop")

    assert os.environ["BOOLEY_EXISTING"] == "before"
    assert "BOOLEY_ADDED" not in os.environ


@pytest.mark.asyncio
async def test_scoped_environment_restores_keys_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOOLEY_EXISTING", "before")
    entered = asyncio.Event()

    async def hold_scope() -> None:
        with scoped_environment({"BOOLEY_EXISTING": "during"}):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_scope())
    await entered.wait()
    assert os.environ["BOOLEY_EXISTING"] == "during"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert os.environ["BOOLEY_EXISTING"] == "before"
