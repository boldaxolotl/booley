"""Focused forwarding and security-contract tests for the FlexNet relay."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import socket
from collections.abc import AsyncIterator

import pytest
from tests.time_budget import hang_guard_s, scaled

from booley.eda.provisioning.licensing import flexnet_relay
from booley.eda.provisioning.licensing.flexnet_relay import (
    ENV_CONNECT_TIMEOUT,
    ENV_IDLE_TIMEOUT,
    ENV_LMGRD_PORT,
    ENV_MAX_CONNECTIONS,
    ENV_SERVER_HOSTID,
    ENV_UPSTREAM_IPV4,
    ENV_VENDOR_PORT,
    FlexNetRelay,
    PortRoute,
    RelayConfig,
    RelayConfigError,
    healthcheck,
)

# Short real windows that must not expire under load grow with the time scale.
_IDLE_S = scaled(1.0)
_CONNECT_S = scaled(5.0)
_RESET_IDLE_S = scaled(0.15)
_WRITE_GAP_S = 0.03


def _env(**overrides: str) -> dict[str, str]:
    values = {
        ENV_UPSTREAM_IPV4: "10.20.30.40",
        ENV_SERVER_HOSTID: "license-server-01",
        ENV_LMGRD_PORT: "2100",
        ENV_VENDOR_PORT: "2101",
    }
    values.update(overrides)
    return values


def test_accepts_fixed_ipv4_hostid_and_distinct_ports() -> None:
    config = RelayConfig.from_env(_env())
    assert config.upstream_ipv4 == ipaddress.IPv4Address("10.20.30.40")
    assert config.server_hostid == "license-server-01"
    assert config.routes == (PortRoute(2100, 2100), PortRoute(2101, 2101))


@pytest.mark.parametrize(
    "name,value,match",
    [
        *[
            (ENV_UPSTREAM_IPV4, value, "literal IPv4|usable IPv4 unicast")
            for value in (
                "licenses.example",
                "::1",
                "0.0.0.0",
                "127.0.0.1",
                "169.254.1.2",
                "224.0.0.1",
                "255.255.255.255",
            )
        ],
        *[
            (ENV_SERVER_HOSTID, value, "Host Identifier|required")
            for value in ("", "-server", "bad\nname", "192.0.2.1")
        ],
        *[(ENV_LMGRD_PORT, value, "TCP port|between") for value in ("two", "0", "65536")],
        (ENV_VENDOR_PORT, "2100", "distinct"),
        *[
            (name, value, "numeric|finite")
            for name in (ENV_CONNECT_TIMEOUT, ENV_IDLE_TIMEOUT)
            for value in ("0", "nan", "not-a-number", "99999")
        ],
        *[
            (ENV_MAX_CONNECTIONS, value, "positive decimal|between")
            for value in ("0", "513", "many")
        ],
    ],
)
def test_rejects_invalid_relay_settings(name: str, value: str, match: str) -> None:
    with pytest.raises(RelayConfigError, match=match):
        RelayConfig.from_env(_env(**{name: value}))


async def _start_server(handler) -> asyncio.AbstractServer:
    try:
        return await asyncio.start_server(handler, "127.0.0.1", 0)
    except OSError as exc:
        pytest.skip(f"loopback sockets unavailable: {exc}")


async def _half_close_echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    payload = await reader.read()
    writer.write(b"reply:" + payload)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def _test_config(
    upstream_ports: tuple[int, int],
    *,
    idle_timeout: float = _IDLE_S,
    maximum: int = 4,
    connect_timeout: float = _CONNECT_S,
) -> RelayConfig:
    return RelayConfig(
        ipaddress.IPv4Address("127.0.0.1"),
        "test-license-host",
        (PortRoute(0, upstream_ports[0]), PortRoute(0, upstream_ports[1])),
        listen_host="127.0.0.1",
        # The 5 s default lets a reachable upstream survive event-loop stalls under
        # load; a short value made the idle-timeout test see "upstream failed".
        connect_timeout=connect_timeout,
        idle_timeout=idle_timeout,
        max_connections=maximum,
        health_port=0,
    )


@contextlib.asynccontextmanager
async def _relay_to(handler, **config) -> AsyncIterator[FlexNetRelay]:
    """Run a started relay whose two routes lead to one upstream server running *handler*."""
    upstream = await _start_server(handler)
    port = upstream.sockets[0].getsockname()[1]
    relay = FlexNetRelay(_test_config((port, port), **config))
    try:
        await relay.start()
        try:
            yield relay
        finally:
            await relay.close()
    finally:
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_relays_both_ports_preserves_half_close_and_never_logs_payload(caplog) -> None:
    caplog.set_level(logging.INFO)
    async with _relay_to(_half_close_echo) as relay:
        for port, payload in zip(
            relay.bound_ports, (b"manager-secret", b"vendor-secret"), strict=True
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            assert await reader.read() == b"reply:" + payload
            writer.close()
            await writer.wait_closed()

    assert "manager-secret" not in caplog.text
    assert "vendor-secret" not in caplog.text
    assert "bytes_up=" in caplog.text
    assert "bytes_down=" in caplog.text


@pytest.mark.asyncio
async def test_health_endpoint_is_loopback_ready_only_while_started() -> None:
    async with _relay_to(_half_close_echo) as relay:
        health_port = relay.health_bound_port
        assert health_port is not None
        assert await asyncio.to_thread(healthcheck, port=health_port)
    assert not await asyncio.to_thread(healthcheck, port=health_port, timeout=0.05)


@pytest.mark.asyncio
async def test_idle_connection_is_closed_with_metadata_only_reason(caplog) -> None:
    release = asyncio.Event()

    async def hanging(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.close()
        await writer.wait_closed()

    caplog.set_level(logging.WARNING)
    async with _relay_to(hanging, idle_timeout=scaled(0.05)) as relay:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.bound_ports[0])
            assert await asyncio.wait_for(reader.read(), timeout=hang_guard_s()) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            release.set()
    assert "reason=idle" in caplog.text


@pytest.mark.asyncio
async def test_activity_in_either_direction_resets_connection_idle_timeout() -> None:
    writes = math.ceil(2 * _RESET_IDLE_S / _WRITE_GAP_S)

    async def streaming(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Streaming lasts twice the idle window, so a relay that never resets it fails.
        assert await reader.readexactly(1) == b"?"
        for _ in range(writes):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(_WRITE_GAP_S)
        writer.close()
        await writer.wait_closed()

    async with _relay_to(streaming, idle_timeout=_RESET_IDLE_S) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.bound_ports[0])
        writer.write(b"?")
        await writer.drain()
        assert (
            await asyncio.wait_for(reader.readexactly(writes), timeout=hang_guard_s())
            == b"x" * writes
        )
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_connection_limit_rejects_excess_without_upstream_attempt(caplog) -> None:
    accepted = asyncio.Event()
    release = asyncio.Event()

    async def holding(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        await release.wait()
        writer.close()
        await writer.wait_closed()

    caplog.set_level(logging.WARNING)
    async with _relay_to(holding, maximum=1) as relay:
        first_reader, first_writer = await asyncio.open_connection(
            "127.0.0.1", relay.bound_ports[0]
        )
        try:
            await asyncio.wait_for(accepted.wait(), timeout=hang_guard_s())
            rejected_reader, rejected_writer = await asyncio.open_connection(
                "127.0.0.1", relay.bound_ports[1]
            )
            assert await asyncio.wait_for(rejected_reader.read(), timeout=hang_guard_s()) == b""
            rejected_writer.close()
            await rejected_writer.wait_closed()
        finally:
            release.set()
            first_writer.close()
            await first_writer.wait_closed()
            await first_reader.read()
    assert "reason=limit" in caplog.text


@pytest.mark.asyncio
async def test_unreachable_upstream_fails_closed_without_destination_in_logs(caplog) -> None:
    # A bound socket that never listens refuses every connection, and holding it
    # keeps a parallel test from taking the port and answering in its place.
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        reservation.bind(("127.0.0.1", 0))
    except OSError as exc:
        reservation.close()
        pytest.skip(f"loopback sockets unavailable: {exc}")
    port = reservation.getsockname()[1]
    # Windows retries a refused loopback connect for about 2 s; a short connect
    # timeout ends the attempt quickly. Refusal and timeout both log "upstream failed".
    relay = FlexNetRelay(_test_config((port, port + 1), connect_timeout=0.2))
    caplog.set_level(logging.WARNING)
    try:
        await relay.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.bound_ports[0])
            try:
                assert await asyncio.wait_for(reader.read(), timeout=hang_guard_s()) == b""
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await relay.close()
    finally:
        reservation.close()
    assert "upstream failed" in caplog.text
    assert "127.0.0.1" not in caplog.text


def test_relay_that_cannot_listen_logs_the_address_and_exits_2(monkeypatch, caplog) -> None:
    with socket.socket() as holder:
        holder.bind(("0.0.0.0", 0))
        holder.listen()
        port = holder.getsockname()[1]
        for name, value in _env(**{ENV_LMGRD_PORT: str(port)}).items():
            monkeypatch.setenv(name, value)

        with caplog.at_level(logging.ERROR):
            assert flexnet_relay.main([]) == 2

    assert any("relay could not listen" in m and str(port) in m for m in caplog.messages)
