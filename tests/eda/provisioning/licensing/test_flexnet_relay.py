"""Focused forwarding and security-contract tests for the FlexNet relay."""

from __future__ import annotations

import asyncio
import ipaddress
import logging

import pytest

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


def _env(**overrides: str) -> dict[str, str]:
    values = {
        ENV_UPSTREAM_IPV4: "10.20.30.40",
        ENV_SERVER_HOSTID: "license-server-01",
        ENV_LMGRD_PORT: "2100",
        ENV_VENDOR_PORT: "2101",
    }
    values.update(overrides)
    return values


class TestRelayConfig:
    def test_accepts_fixed_ipv4_hostid_and_distinct_ports(self) -> None:
        config = RelayConfig.from_env(_env())
        assert config.upstream_ipv4 == ipaddress.IPv4Address("10.20.30.40")
        assert config.server_hostid == "license-server-01"
        assert config.routes == (PortRoute(2100, 2100), PortRoute(2101, 2101))

    @pytest.mark.parametrize(
        "value",
        [
            "licenses.example",
            "::1",
            "0.0.0.0",
            "127.0.0.1",
            "169.254.1.2",
            "224.0.0.1",
            "255.255.255.255",
        ],
    )
    def test_rejects_nonliteral_or_unsupported_ipv4(self, value: str) -> None:
        with pytest.raises(RelayConfigError, match=r"literal IPv4|IPv4 unicast"):
            RelayConfig.from_env(_env(**{ENV_UPSTREAM_IPV4: value}))

    @pytest.mark.parametrize(
        "value", ["", "-server", "server-", "license/server", "192.0.2.1", "bad\nname"]
    )
    def test_rejects_invalid_server_host_identifier(self, value: str) -> None:
        with pytest.raises(RelayConfigError, match=r"Host Identifier|required"):
            RelayConfig.from_env(_env(**{ENV_SERVER_HOSTID: value}))

    @pytest.mark.parametrize("value", ["0", "65536", "two", "-1"])
    def test_rejects_invalid_ports(self, value: str) -> None:
        with pytest.raises(RelayConfigError, match=r"TCP port|between"):
            RelayConfig.from_env(_env(**{ENV_LMGRD_PORT: value}))

    def test_rejects_duplicate_ports(self) -> None:
        with pytest.raises(RelayConfigError, match="distinct"):
            RelayConfig.from_env(_env(**{ENV_VENDOR_PORT: "2100"}))

    @pytest.mark.parametrize("name", [ENV_CONNECT_TIMEOUT, ENV_IDLE_TIMEOUT])
    @pytest.mark.parametrize("value", ["0", "nan", "inf", "not-a-number", "99999"])
    def test_rejects_unbounded_timeouts(self, name: str, value: str) -> None:
        with pytest.raises(RelayConfigError, match=r"numeric|finite"):
            RelayConfig.from_env(_env(**{name: value}))

    @pytest.mark.parametrize("value", ["0", "513", "1.5", "many"])
    def test_rejects_unbounded_connection_limits(self, value: str) -> None:
        with pytest.raises(RelayConfigError, match=r"positive decimal|between"):
            RelayConfig.from_env(_env(**{ENV_MAX_CONNECTIONS: value}))


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
    upstream_ports: tuple[int, int], *, idle_timeout: float = 1.0, maximum: int = 4
) -> RelayConfig:
    return RelayConfig(
        ipaddress.IPv4Address("127.0.0.1"),
        "test-license-host",
        (PortRoute(0, upstream_ports[0]), PortRoute(0, upstream_ports[1])),
        listen_host="127.0.0.1",
        connect_timeout=0.2,
        idle_timeout=idle_timeout,
        max_connections=maximum,
        health_port=0,
    )


@pytest.mark.asyncio
async def test_relays_both_ports_preserves_half_close_and_never_logs_payload(caplog) -> None:
    first = await _start_server(_half_close_echo)
    second = await _start_server(_half_close_echo)
    upstream = (first.sockets[0].getsockname()[1], second.sockets[0].getsockname()[1])
    relay = FlexNetRelay(_test_config(upstream))
    caplog.set_level(logging.INFO)
    await relay.start()
    try:
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
    finally:
        await relay.close()
        first.close()
        second.close()
        await asyncio.gather(first.wait_closed(), second.wait_closed())

    assert "manager-secret" not in caplog.text
    assert "vendor-secret" not in caplog.text
    assert "bytes_up=" in caplog.text
    assert "bytes_down=" in caplog.text


@pytest.mark.asyncio
async def test_health_endpoint_is_loopback_ready_only_while_started() -> None:
    upstream = await _start_server(_half_close_echo)
    port = upstream.sockets[0].getsockname()[1]
    relay = FlexNetRelay(_test_config((port, port)))
    await relay.start()
    health_port = relay.health_bound_port
    assert health_port is not None
    try:
        assert await asyncio.to_thread(healthcheck, port=health_port)
    finally:
        await relay.close()
        upstream.close()
        await upstream.wait_closed()
    assert not await asyncio.to_thread(healthcheck, port=health_port, timeout=0.05)


@pytest.mark.asyncio
async def test_idle_connection_is_closed_with_metadata_only_reason(caplog) -> None:
    release = asyncio.Event()

    async def hanging(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.close()
        await writer.wait_closed()

    upstream = await _start_server(hanging)
    port = upstream.sockets[0].getsockname()[1]
    relay = FlexNetRelay(_test_config((port, port), idle_timeout=0.05))
    caplog.set_level(logging.WARNING)
    await relay.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.bound_ports[0])
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        release.set()
        await relay.close()
        upstream.close()
        await upstream.wait_closed()
    assert "reason=idle" in caplog.text


@pytest.mark.asyncio
async def test_activity_in_either_direction_resets_connection_idle_timeout() -> None:
    async def streaming(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readexactly(1) == b"?"
        for _ in range(10):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.03)
        writer.close()
        await writer.wait_closed()

    upstream = await _start_server(streaming)
    port = upstream.sockets[0].getsockname()[1]
    relay = FlexNetRelay(_test_config((port, port), idle_timeout=0.15))
    await relay.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.bound_ports[0])
        writer.write(b"?")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(10), timeout=2) == b"xxxxxxxxxx"
        writer.close()
        await writer.wait_closed()
    finally:
        await relay.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_connection_limit_rejects_excess_without_upstream_attempt(caplog) -> None:
    accepted = asyncio.Event()
    release = asyncio.Event()

    async def holding(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        await release.wait()
        writer.close()
        await writer.wait_closed()

    upstream = await _start_server(holding)
    port = upstream.sockets[0].getsockname()[1]
    relay = FlexNetRelay(_test_config((port, port), maximum=1))
    caplog.set_level(logging.WARNING)
    await relay.start()
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", relay.bound_ports[0])
    await asyncio.wait_for(accepted.wait(), timeout=1)
    try:
        rejected_reader, rejected_writer = await asyncio.open_connection(
            "127.0.0.1", relay.bound_ports[1]
        )
        assert await asyncio.wait_for(rejected_reader.read(), timeout=1) == b""
        rejected_writer.close()
        await rejected_writer.wait_closed()
    finally:
        release.set()
        first_writer.close()
        await first_writer.wait_closed()
        await first_reader.read()
        await relay.close()
        upstream.close()
        await upstream.wait_closed()
    assert "reason=limit" in caplog.text


@pytest.mark.asyncio
async def test_unreachable_upstream_fails_closed_without_destination_in_logs(caplog) -> None:
    reservation = await _start_server(_half_close_echo)
    port = reservation.sockets[0].getsockname()[1]
    reservation.close()
    await reservation.wait_closed()
    relay = FlexNetRelay(_test_config((port, port + 1)))
    caplog.set_level(logging.WARNING)
    await relay.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.bound_ports[0])
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await relay.close()
    assert "upstream failed" in caplog.text
    assert "127.0.0.1" not in caplog.text
