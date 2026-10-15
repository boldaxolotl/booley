"""SDK change delivery and lifespan ownership for one live MCP catalog."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.context import CallNext, ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp.types import ServerCapabilities
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from booley.mcp.live_catalog import LiveCatalog

logger = logging.getLogger(__name__)
_POLL_SECONDS = 0.25
_SEND_TIMEOUT_SECONDS = 1.0


class ChangingToolsServer(Server):
    """Advertise legacy list changes through the SDK's default HTTP init path too."""

    def get_capabilities(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
        *,
        protocol_version: str | None = None,
    ) -> ServerCapabilities:
        capabilities = super().get_capabilities(
            notification_options,
            experimental_capabilities,
            extensions,
            protocol_version=protocol_version,
        )
        if capabilities.tools is not None:
            capabilities.tools.list_changed = True
        return capabilities


class ToolChanges:
    """Bounded modern subscription fanout and persistent legacy peer delivery."""

    def __init__(self, catalog: LiveCatalog) -> None:
        self.catalog: LiveCatalog = catalog
        self.bus: InMemorySubscriptionBus = InMemorySubscriptionBus()
        self.listen: ListenHandler = ListenHandler(self.bus)
        self._peers: dict[int, Any] = {}
        self._running = False

    async def remember_legacy_peer(self, ctx: ServerRequestContext, call_next: CallNext) -> Any:
        result = await call_next(ctx)
        if ctx.method == "initialize" and ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            # Pinned SDK low-level contexts expose ServerSession, not Context.connection.
            # Its private connection is the one compatibility read; the public
            # Connection API owns standalone notification delivery and cleanup.
            connection = ctx.session._connection
            key = id(connection)
            if key not in self._peers:
                self._peers[key] = connection
                connection.exit_stack.callback(self._peers.pop, key, None)
        return result

    async def publish(self) -> None:
        """A broken/slow recipient cannot block the remaining clients."""
        await self.bus.publish(ToolsListChanged())
        async with anyio.create_task_group() as group:
            for key, connection in tuple(self._peers.items()):
                group.start_soon(self._send, key, connection)

    async def _send(self, key: int, connection: Any) -> None:
        try:
            with anyio.fail_after(_SEND_TIMEOUT_SECONDS):
                await connection.send_tool_list_changed()
        except Exception:
            self._peers.pop(key, None)
            logger.debug("MCP tools change recipient closed or timed out", exc_info=True)

    async def _poll(self) -> None:
        while self._running:
            await asyncio.sleep(_POLL_SECONDS)
            try:
                if await self.catalog.observe_change():
                    # Invalidate before bounded discovery can wait on Project code.
                    await self.publish()
                    await self.catalog.refresh()
            except Exception:
                logger.warning("MCP tools change observation failed", exc_info=True)

    @asynccontextmanager
    async def lifespan(self, _server: Server) -> AsyncIterator[dict[str, Any]]:
        """The SDK owns one poller for stdio, HTTP, and in-process lifetimes."""
        if self._running:
            raise RuntimeError("MCP tool change lifespan is already running")
        self._running = True
        async with anyio.create_task_group() as group:
            group.start_soon(self._poll)
            try:
                yield {}
            finally:
                self._running = False
                group.cancel_scope.cancel()
                with anyio.CancelScope(shield=True):
                    self.listen.close()
                    self._peers.clear()
