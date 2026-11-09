"""Tests for egress_proxy — allowlist, url, auth, _is_allowed, DEFAULT_ALLOWLIST, tunneling."""

from __future__ import annotations

import asyncio
import base64

import pytest

from booley.docker.egress_proxy import DEFAULT_ALLOWLIST, EgressProxy

# ---------------------------------------------------------------------------
# EgressProxy.__init__
# ---------------------------------------------------------------------------


class TestEgressProxyInit:
    def test_default_allowlist(self):
        proxy = EgressProxy()
        assert proxy.allowlist == set(DEFAULT_ALLOWLIST)

    def test_custom_allowlist_extends_defaults(self):
        # Caller-supplied domains extend (not replace) DEFAULT_ALLOWLIST — see the
        # "extend not replace" semantics documented in EgressProxy.__init__.
        proxy = EgressProxy(allowlist=["example.com", "foo.org"])
        assert proxy.allowlist == set(DEFAULT_ALLOWLIST) | {"example.com", "foo.org"}


# ---------------------------------------------------------------------------
# EgressProxy.url property
# ---------------------------------------------------------------------------


class TestUrl:
    def test_url_format_after_port_set(self):
        proxy = EgressProxy()
        proxy.port = 9999
        assert proxy.url == "http://host.docker.internal:9999"

    def test_authenticated_url_carries_private_proxy_credential(self):
        proxy = EgressProxy(auth_token="private-token")
        proxy.port = 1234
        assert proxy.url == "http://booley:private-token@host.docker.internal:1234"


class TestProxyAuthentication:
    def test_no_configured_token_accepts_missing_header(self):
        assert EgressProxy()._proxy_authorized([]) is True

    def test_matching_basic_credential_is_accepted(self):
        proxy = EgressProxy(auth_token="private-token")
        encoded = base64.b64encode(b"booley:private-token")
        assert proxy._proxy_authorized([b"Proxy-Authorization: Basic " + encoded + b"\r\n"])

    def test_missing_invalid_and_duplicate_credentials_are_rejected(self):
        proxy = EgressProxy(auth_token="private-token")
        valid = b"Proxy-Authorization: Basic " + base64.b64encode(b"booley:private-token")
        assert proxy._proxy_authorized([]) is False
        assert proxy._proxy_authorized([b"Proxy-Authorization: Basic wrong\r\n"]) is False
        assert proxy._proxy_authorized([valid, valid]) is False


# ---------------------------------------------------------------------------
# EgressProxy._is_allowed
# ---------------------------------------------------------------------------


class TestIsAllowed:
    def test_subdomain_match(self):
        proxy = EgressProxy(allowlist=["example.com"])
        assert proxy._is_allowed("sub.example.com") is True
        assert proxy._is_allowed("deep.sub.example.com") is True

    def test_case_insensitivity(self):
        proxy = EgressProxy(allowlist=["Example.COM"])
        assert proxy._is_allowed("example.com") is True
        assert proxy._is_allowed("EXAMPLE.COM") is True
        assert proxy._is_allowed("Example.Com") is True

    def test_dot_stripping(self):
        """Trailing dots (FQDN notation) should be stripped."""
        proxy = EgressProxy(allowlist=["example.com"])
        assert proxy._is_allowed("example.com.") is True

    def test_dot_stripping_in_allowlist(self):
        """Allowlist entries with trailing dots should still match."""
        proxy = EgressProxy(allowlist=["example.com."])
        assert proxy._is_allowed("example.com") is True

    def test_rejection_of_non_allowed_domain(self):
        proxy = EgressProxy(allowlist=["example.com"])
        assert proxy._is_allowed("evil.com") is False
        assert proxy._is_allowed("notexample.com") is False

    def test_multiple_allowlist_entries(self):
        proxy = EgressProxy(allowlist=["a.com", "b.org", "c.io"])
        assert proxy._is_allowed("a.com") is True
        assert proxy._is_allowed("sub.b.org") is True
        assert proxy._is_allowed("c.io") is True
        assert proxy._is_allowed("d.net") is False


# ---------------------------------------------------------------------------
# DEFAULT_ALLOWLIST contents
# ---------------------------------------------------------------------------


class TestDefaultAllowlist:
    def test_expected_domains_present(self):
        expected = [
            "api.anthropic.com",
            "api.openai.com",
            "auth.anthropic.com",
            "sentry.io",
        ]
        for domain in expected:
            assert domain in DEFAULT_ALLOWLIST, f"{domain} missing from DEFAULT_ALLOWLIST"

    def test_oauth_login_endpoints_present(self):
        """Both backends' *login* hosts, not just their inference hosts.

        A missing auth host fails silently until a user tries to sign in, and
        then only at the final token-exchange step — the failure mode that cost
        us a Codex sign-in ("token_exchange_failed" against auth.openai.com).
        """
        for domain in ("auth.anthropic.com", "claude.ai", "auth.openai.com"):
            assert domain in DEFAULT_ALLOWLIST, f"{domain} missing from DEFAULT_ALLOWLIST"

    def test_no_scheme_prefixes(self):
        """Entries should be bare hostnames, not URLs."""
        for domain in DEFAULT_ALLOWLIST:
            assert "://" not in domain, f"{domain} looks like a URL, not a hostname"


# ---------------------------------------------------------------------------
# CONNECT tunnel — regression for indefinitely-idle streaming directions
# ---------------------------------------------------------------------------


async def _drain_line(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.readline(), timeout=5)


class TestTunnelIdleDirection:
    """A CONNECT tunnel must survive a direction that stays idle for a long time.

    Regression for the container hang where Claude Code sat "thinking" forever:
    the client->server (request) direction is silent for the whole model turn
    while the server streams the response back. An idle-read timeout that closed
    the peer used to tear that live stream down mid-response.
    """

    @pytest.mark.asyncio
    async def test_missing_proxy_auth_is_rejected_without_secret_disclosure(self):
        proxy = EgressProxy(
            allowlist=["127.0.0.1"],
            host="127.0.0.1",
            auth_token="private-token",
        )
        proxy._server = await asyncio.start_server(proxy._handle_client, "127.0.0.1", 0)
        proxy.port = proxy._server.sockets[0].getsockname()[1]

        async with proxy._server:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
            await writer.drain()
            status = await _drain_line(reader)
            assert b"407" in status
            assert b"private-token" not in status
            assert proxy.stats.blocked == 1
            writer.close()

    @pytest.mark.asyncio
    async def test_idle_request_direction_does_not_kill_response_stream(self):
        # Upstream "Anthropic": accepts a connection, then dribbles bytes back to
        # the client while never expecting anything more on the request side.
        server_ready = asyncio.Event()
        upstream_port = 0

        async def upstream_handler(reader, writer):
            # Read the initial request bytes the client sends once, then stream.
            await reader.read(1)
            for i in range(3):
                writer.write(f"chunk{i}\n".encode())
                await writer.drain()
                await asyncio.sleep(0.05)
            writer.close()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]

        proxy = EgressProxy(
            allowlist=["127.0.0.1"],
            host="127.0.0.1",
            auth_token="private-token",
        )

        # Run the proxy server on this test's own event loop (not its thread) so we
        # can await it directly; start_server binds an ephemeral port.
        proxy._server = await asyncio.start_server(proxy._handle_client, "127.0.0.1", 0)
        proxy.port = proxy._server.sockets[0].getsockname()[1]
        server_ready.set()

        async with upstream, proxy._server:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            # Establish the CONNECT tunnel to our fake upstream.
            credential = base64.b64encode(b"booley:private-token").decode()
            writer.write(
                (
                    f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n"
                    f"Proxy-Authorization: Basic {credential}\r\n\r\n"
                ).encode()
            )
            await writer.drain()

            status = await _drain_line(reader)
            assert b"200" in status, status
            # consume the blank line after the status line
            await _drain_line(reader)

            # Client sends its one request byte, then goes SILENT (the idle
            # request direction). The response must still stream through intact.
            writer.write(b"x")
            await writer.drain()

            received = b""
            while b"chunk2" not in received:
                data = await asyncio.wait_for(reader.read(1024), timeout=5)
                if not data:
                    break
                received += data

            assert b"chunk0" in received
            assert b"chunk2" in received  # full stream delivered, not truncated
            assert proxy.stats.allowed == 1
            writer.close()
