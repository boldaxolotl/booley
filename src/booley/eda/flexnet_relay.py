"""Fixed-destination, metadata-only TCP relay for Xilinx FlexNet licensing."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import math
import os
import re
import socket
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass

BUFFER_SIZE = 64 * 1024
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_MAX_CONNECTIONS = 32
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 18080
LOG_FORMAT = "level=%(levelname)s %(message)s"

ENV_UPSTREAM_IPV4 = "BOOLEY_FLEXNET_UPSTREAM_IPV4"
ENV_SERVER_HOSTID = "BOOLEY_FLEXNET_SERVER_HOSTID"
ENV_LMGRD_PORT = "BOOLEY_FLEXNET_LMGRD_PORT"
ENV_VENDOR_PORT = "BOOLEY_FLEXNET_VENDOR_PORT"
ENV_CONNECT_TIMEOUT = "BOOLEY_FLEXNET_CONNECT_TIMEOUT"
ENV_IDLE_TIMEOUT = "BOOLEY_FLEXNET_IDLE_TIMEOUT"
ENV_MAX_CONNECTIONS = "BOOLEY_FLEXNET_MAX_CONNECTIONS"

_HOST_ID_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


class RelayConfigError(ValueError):
    """The fixed FlexNet relay contract is incomplete or unsafe."""


class RelayIdleTimeoutError(TimeoutError):
    """No bytes crossed one relay direction within the configured bound."""


@dataclass(frozen=True)
class PortRoute:
    """One fixed listener-to-upstream port route."""

    listen_port: int
    upstream_port: int


@dataclass(frozen=True)
class RelayConfig:
    """Validated relay boundary with one literal destination and two ports."""

    upstream_ipv4: ipaddress.IPv4Address
    server_hostid: str
    routes: tuple[PortRoute, PortRoute]
    listen_host: str = "0.0.0.0"
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    health_port: int = HEALTH_PORT

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> RelayConfig:
        """Parse the relay's deliberately narrow environment contract."""
        upstream = validate_upstream_ipv4(_required(env, ENV_UPSTREAM_IPV4))
        hostid = validate_server_hostid(_required(env, ENV_SERVER_HOSTID))
        lmgrd = _port(_required(env, ENV_LMGRD_PORT), ENV_LMGRD_PORT)
        vendor = _port(_required(env, ENV_VENDOR_PORT), ENV_VENDOR_PORT)
        if lmgrd == vendor:
            raise RelayConfigError("lmgrd and vendor ports must be distinct")
        connect_timeout = _positive_float(env, ENV_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT, 60.0)
        idle_timeout = _positive_float(env, ENV_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT, 3600.0)
        maximum = _positive_int(env, ENV_MAX_CONNECTIONS, DEFAULT_MAX_CONNECTIONS, 512)
        return cls(
            upstream,
            hostid,
            (PortRoute(lmgrd, lmgrd), PortRoute(vendor, vendor)),
            connect_timeout=connect_timeout,
            idle_timeout=idle_timeout,
            max_connections=maximum,
        )


def validate_upstream_ipv4(value: str) -> ipaddress.IPv4Address:
    """Return one literal usable IPv4 unicast address or raise."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise RelayConfigError("FlexNet upstream must be a literal IPv4 address") from exc
    forbidden = (
        address.is_unspecified
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )
    if not isinstance(address, ipaddress.IPv4Address) or forbidden:
        raise RelayConfigError("FlexNet upstream must be a usable IPv4 unicast address")
    return address


def validate_server_hostid(value: str) -> str:
    """Validate the literal FlexNet ``SERVER`` Host Identifier used as an alias."""
    if not _HOST_ID_RE.fullmatch(value) or value.replace(".", "").isdigit():
        raise RelayConfigError("FlexNet SERVER Host Identifier is invalid")
    return value


class FlexNetRelay:
    """Two-port TCP relay with bounded connections, buffers, and idle time."""

    def __init__(self, config: RelayConfig) -> None:
        self.config = config
        self._route_servers: list[asyncio.AbstractServer] = []
        self._health_server: asyncio.AbstractServer | None = None
        self._active_connections = 0
        self._connection_serial = 0

    @property
    def bound_ports(self) -> tuple[int, ...]:
        """Return the actual route listener ports after startup."""
        return tuple(server.sockets[0].getsockname()[1] for server in self._route_servers)

    @property
    def health_bound_port(self) -> int | None:
        """Return the local health port while the relay is ready."""
        if self._health_server is None:
            return None
        return self._health_server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        """Bind every listener atomically; close partial startup on failure."""
        try:
            for route in self.config.routes:
                server = await asyncio.start_server(
                    lambda reader, writer, selected=route: self._handle(reader, writer, selected),
                    self.config.listen_host,
                    route.listen_port,
                    limit=BUFFER_SIZE,
                    backlog=self.config.max_connections,
                )
                self._route_servers.append(server)
                logging.info("relay listening listener_port=%d", self.bound_ports[-1])
            self._health_server = await asyncio.start_server(
                self._handle_health,
                HEALTH_HOST,
                self.config.health_port,
                limit=1024,
                backlog=4,
            )
            logging.info("relay ready routes=%d", len(self._route_servers))
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Stop accepting route and health connections."""
        servers = [*self._route_servers]
        if self._health_server is not None:
            servers.append(self._health_server)
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))
        self._route_servers.clear()
        self._health_server = None

    async def serve_forever(self) -> None:
        """Start all listeners and serve until cancellation or termination."""
        await self.start()
        assert self._health_server is not None
        servers = [*self._route_servers, self._health_server]
        try:
            await asyncio.gather(*(server.serve_forever() for server in servers))
        finally:
            await self.close()

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        route: PortRoute,
    ) -> None:
        self._connection_serial += 1
        connection = self._connection_serial
        listener = client_writer.get_extra_info("sockname")[1]
        if self._active_connections >= self.config.max_connections:
            logging.warning(
                "connection rejected id=%d listener_port=%d reason=limit", connection, listener
            )
            await _close_writer(client_writer)
            return
        self._active_connections += 1
        try:
            await self._forward(connection, listener, route, client_reader, client_writer)
        finally:
            self._active_connections -= 1

    async def _forward(
        self,
        connection: int,
        listener: int,
        route: PortRoute,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(str(self.config.upstream_ipv4), route.upstream_port),
                timeout=self.config.connect_timeout,
            )
        except (TimeoutError, OSError) as exc:
            logging.warning(
                "upstream failed id=%d listener_port=%d error=%s",
                connection,
                listener,
                type(exc).__name__,
            )
            await _close_writer(client_writer)
            return
        logging.info("connection opened id=%d listener_port=%d", connection, listener)
        try:
            counts = await _relay_bidirectional(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
                self.config.idle_timeout,
            )
            logging.info(
                "connection closed id=%d listener_port=%d bytes_up=%d bytes_down=%d",
                connection,
                listener,
                counts[0],
                counts[1],
            )
        except RelayIdleTimeoutError:
            logging.warning(
                "connection closed id=%d listener_port=%d reason=idle", connection, listener
            )
        except (ConnectionError, OSError) as exc:
            logging.warning(
                "connection closed id=%d listener_port=%d error=%s",
                connection,
                listener,
                type(exc).__name__,
            )
        finally:
            await asyncio.gather(_close_writer(client_writer), _close_writer(upstream_writer))

    async def _handle_health(
        self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"READY\n")
        await writer.drain()
        await _close_writer(writer)


async def _pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    activity: asyncio.Queue[None],
) -> int:
    transferred = 0
    while True:
        data = await reader.read(BUFFER_SIZE)
        if not data:
            break
        transferred += len(data)
        writer.write(data)
        await writer.drain()
        if activity.empty():
            activity.put_nowait(None)
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()
    return transferred


async def _relay_bidirectional(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    idle_timeout: float,
) -> tuple[int, int]:
    activity: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    tasks = {
        asyncio.create_task(_pump(client_reader, upstream_writer, activity)): 0,
        asyncio.create_task(_pump(upstream_reader, client_writer, activity)): 1,
    }
    counts = [0, 0]
    try:
        while tasks:
            activity_waiter = asyncio.create_task(activity.get())
            done, _ = await asyncio.wait(
                [*tasks, activity_waiter],
                timeout=idle_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                activity_waiter.cancel()
                raise RelayIdleTimeoutError
            if activity_waiter not in done:
                activity_waiter.cancel()
            for task in done - {activity_waiter}:
                counts[tasks.pop(task)] = task.result()
        return counts[0], counts[1]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(ConnectionError, OSError):
        await writer.wait_closed()


def healthcheck(*, port: int = HEALTH_PORT, timeout: float = 2.0) -> bool:
    """Return whether the relay's loopback-only readiness endpoint responds."""
    try:
        with socket.create_connection((HEALTH_HOST, port), timeout=timeout) as stream:
            stream.settimeout(timeout)
            return stream.recv(16) == b"READY\n"
    except OSError:
        return False


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RelayConfigError(f"{name} is required")
    return value


def _port(raw: str, name: str) -> int:
    if not raw.isascii() or not raw.isdecimal():
        raise RelayConfigError(f"{name} must be a decimal TCP port")
    value = int(raw)
    if not 1 <= value <= 65535:
        raise RelayConfigError(f"{name} must be between 1 and 65535")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float, maximum: float) -> float:
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RelayConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise RelayConfigError(f"{name} must be finite, positive, and at most {maximum:g}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    raw = env.get(name, str(default))
    if not raw.isascii() or not raw.isdecimal():
        raise RelayConfigError(f"{name} must be a positive decimal integer")
    value = int(raw)
    if not 1 <= value <= maximum:
        raise RelayConfigError(f"{name} must be between 1 and {maximum}")
    return value


def main(argv: list[str] | None = None) -> int:
    """Run the relay or its Docker health probe."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return 0 if healthcheck() else 1
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        config = RelayConfig.from_env()
    except RelayConfigError as exc:
        logging.error("invalid FlexNet relay configuration: %s", exc)
        return 2
    try:
        asyncio.run(FlexNetRelay(config).serve_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
