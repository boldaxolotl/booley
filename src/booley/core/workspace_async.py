"""Join filesystem workers before releasing their task's scratch authority."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from booley.core.temporary_workspace import _ACTIVE, temporary_workspace

T = TypeVar("T")


async def joined(task: asyncio.Task[T]) -> T:
    """Delay cancellation until a finite worker has actually stopped."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def filesystem_call(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run blocking filesystem work with copied context and joined lifetime."""
    return await joined(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))


@contextlib.asynccontextmanager
async def async_workspace(**kwargs: Any) -> AsyncIterator[str]:
    """Allocate and release off-loop while the caller retains its lease context."""
    context = contextvars.copy_context()
    manager = temporary_workspace(**kwargs)
    entered = False
    token = None

    def enter() -> str:
        nonlocal entered
        result = manager.__enter__()
        entered = True
        return result

    try:
        root: str = await filesystem_call(lambda: context.run(enter))
        token = _ACTIVE.set(context.run(_ACTIVE.get))
        yield root
    finally:
        if token is not None:
            _ACTIVE.reset(token)
        if entered:
            await filesystem_call(context.run, manager.__exit__, None, None, None)
