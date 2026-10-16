"""Join filesystem workers before releasing their task's scratch authority."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import AsyncIterator, Callable, Coroutine
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
        except BaseException as error:
            if cancellation is not None:
                raise cancellation from error
            raise
    if cancellation is not None:
        try:
            task.result()
        except BaseException as error:
            raise cancellation from error
        raise cancellation
    return task.result()


async def filesystem_call(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run blocking filesystem work with copied context and joined lifetime."""
    return await joined(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))


async def acquire_task(
    task: asyncio.Task[T], rollback: Callable[[T], Coroutine[Any, Any, None]]
) -> T:
    """Roll back a completed acquisition before propagating caller cancellation."""
    try:
        return await joined(task)
    except asyncio.CancelledError:
        if not task.cancelled() and task.exception() is None:
            await joined(asyncio.create_task(rollback(task.result())))
        raise


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
