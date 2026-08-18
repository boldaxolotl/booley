"""Lightweight HTTPS CONNECT proxy with domain allowlist.

Only allows outbound connections to explicitly allowlisted domains.
All other connection attempts are rejected with 403. Designed to run
on the host and be used by Docker containers via HTTP_PROXY/HTTPS_PROXY.

Usage:
    proxy = EgressProxy(allowlist=["api.anthropic.com", "api.openai.com"])
    proxy.start()       # starts in background thread
    print(proxy.url)    # http://host.docker.internal:<port>
    ...
    proxy.stop()

For curl inside a container:
    HTTPS_PROXY=http://host.docker.internal:<port>
    HTTP_PROXY=http://host.docker.internal:<port>
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import socket
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
    """Turn on TCP keepalive so genuinely dead peers get reaped by the OS.

    The tunnel has no application-level idle timeout (see ``_tunnel``), so
    keepalive is what detects a peer that vanished without sending a FIN/RST.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError as e:  # pragma: no cover - platform dependent
        logger.debug("Failed to enable SO_KEEPALIVE: %s", e)


DEFAULT_ALLOWLIST = [
    # Anthropic / Claude Code
    "api.anthropic.com",  # Claude API (model inference)
    "auth.anthropic.com",
    "mcp-proxy.anthropic.com",
    "statsig.anthropic.com",
    "claude.ai",  # Claude.ai subscription OAuth login
    "platform.claude.com",  # Console/team auth + connectivity check (CC 2.1.x)
    "downloads.claude.ai",  # Native installer / plugin / auto-updater
    # OpenAI / Codex backend
    "api.openai.com",
    "files.openai.com",
    "chatgpt.com",
    # Codex ChatGPT-subscription login: the browser leg runs on the host, but
    # the CLI itself POSTs the authorization code to auth.openai.com/oauth/token
    # from inside the container. Without this the sign-in dies at the very last
    # step with "token_exchange_failed".
    "auth.openai.com",
    # Telemetry / error reporting
    "http-intake.logs.us5.datadoghq.com",
    "sentry.io",
]


@dataclass
class ProxyStats:
    """Connection statistics for the proxy."""

    allowed: int = 0
    blocked: int = 0
    errors: int = 0
    blocked_log: list[str] = field(default_factory=list)


class EgressProxy:
    """CONNECT proxy that enforces a domain allowlist."""

    def __init__(
        self,
        allowlist: list[str] | None = None,
        port: int = 0,
        host: str = "127.0.0.1",
        auth_token: str | None = None,
    ) -> None:
        # Caller-supplied domains *extend* the built-in defaults (they do not
        # replace them) — matches PROXY_ALLOWLIST semantics documented in
        # proxy_entry.py and config.egress_allowlist.
        self.allowlist = set(DEFAULT_ALLOWLIST) | set(allowlist or [])
        self._host = host
        self._auth_token = auth_token
        self._requested_port = port
        self.port: int = 0
        self.stats = ProxyStats()
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def url(self) -> str:
        credentials = f"booley:{self._auth_token}@" if self._auth_token else ""
        return f"http://{credentials}host.docker.internal:{self.port}"

    def _proxy_authorized(self, headers: list[bytes]) -> bool:
        """Return whether *headers* carry this proxy's private credential."""
        if self._auth_token is None:
            return True
        expected = b"Basic " + base64.b64encode(f"booley:{self._auth_token}".encode())
        supplied = [
            line.split(b":", 1)[1].strip()
            for line in headers
            if b":" in line and line.split(b":", 1)[0].strip().lower() == b"proxy-authorization"
        ]
        return len(supplied) == 1 and hmac.compare_digest(supplied[0], expected)

    async def _reject_unauthorized(self, writer: asyncio.StreamWriter, peer: tuple) -> None:
        """Reject a client without disclosing the proxy credential."""
        self.stats.blocked += 1
        self.stats.blocked_log.append("AUTH required")
        logger.debug("BLOCKED: missing/invalid proxy authentication from %s", peer)
        writer.write(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b'Proxy-Authenticate: Basic realm="booley-egress"\r\n'
            b"Content-Length: 0\r\n"
            b"\r\n"
        )
        await writer.drain()
        writer.close()

    def _is_allowed(self, hostname: str) -> bool:
        """Check if hostname matches the allowlist (exact or subdomain)."""
        hostname = hostname.lower().strip(".")
        for allowed in self.allowlist:
            norm = allowed.lower().strip(".")
            if hostname == norm or hostname.endswith("." + norm):
                return True
        return False

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        """Handle a single proxy connection."""
        peer = writer.get_extra_info("peername", ("?", 0))
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_str.split()

            if len(parts) < 2:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                return

            headers = await self._read_remaining_headers(reader)
            if not self._proxy_authorized(headers):
                await self._reject_unauthorized(writer, peer)
                return

            method = parts[0].upper()

            if method == "CONNECT":
                await self._handle_connect(parts[1], reader, writer, peer)
            else:
                await self._handle_forward(request_str, headers, reader, writer, peer)

        except TimeoutError:
            self.stats.errors += 1
            try:
                writer.close()
            except Exception as e:  # noqa: BLE001 — best-effort writer teardown; close failure only logged
                logger.debug("Failed to close writer after timeout from %s: %s", peer, e)
        except Exception as e:  # noqa: BLE001 — top-level proxy guard; logs and closes, must not crash serve loop
            self.stats.errors += 1
            logger.debug("Proxy error from %s: %s", peer, e)
            try:
                writer.close()
            except Exception as close_err:  # noqa: BLE001 — best-effort writer teardown; close failure only logged
                logger.debug("Failed to close writer after error from %s: %s", peer, close_err)

    @staticmethod
    def _parse_connect_target(target: str) -> tuple[str, int]:
        """Parse hostname and port from a CONNECT target string."""
        if ":" in target:
            hostname, port_str = target.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 443
        else:
            hostname = target
            port = 443
        return hostname, port

    async def _reject_blocked(
        self, writer: asyncio.StreamWriter, method: str, hostname: str, port: int, peer: tuple
    ) -> None:
        """Send 403 response for blocked connections."""
        self.stats.blocked += 1
        self.stats.blocked_log.append(f"{method} {hostname}:{port}")
        logger.debug("BLOCKED: %s %s:%d from %s", method, hostname, port, peer)
        writer.write(
            b"HTTP/1.1 403 Forbidden\r\n"
            b"Content-Length: 37\r\n"
            b"\r\n"
            b"Blocked by egress proxy: not allowed\r\n"
        )
        await writer.drain()
        writer.close()

    async def _open_target(
        self,
        hostname: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Open connection to target, returning None on failure."""
        try:
            result = await asyncio.wait_for(asyncio.open_connection(hostname, port), timeout=15)
        except (OSError, TimeoutError) as e:
            self.stats.errors += 1
            logger.debug("Failed to connect to %s:%d: %s", hostname, port, e)
            return None
        _enable_keepalive(result[1])
        return result

    async def _handle_connect(
        self,
        target: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: tuple,
    ):
        """Handle CONNECT method (HTTPS tunneling)."""
        hostname, port = self._parse_connect_target(target)

        if not self._is_allowed(hostname):
            await self._reject_blocked(writer, "CONNECT", hostname, port, peer)
            return

        result = await self._open_target(hostname, port)
        if result is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        target_reader, target_writer = result
        self.stats.allowed += 1
        logger.debug("ALLOWED: CONNECT %s:%d from %s", hostname, port, peer)

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._tunnel(reader, writer, target_reader, target_writer)

    @staticmethod
    def _parse_proxy_url(url: str) -> tuple[str, int, str] | None:
        """Parse hostname, port, path from a proxy URL. Returns None if invalid."""
        if "://" not in url:
            return None
        after_scheme = url.split("://", 1)[1]
        host_part = after_scheme.split("/", 1)[0]
        hostname = host_part.split(":")[0]
        try:
            port = int(host_part.split(":")[1]) if ":" in host_part else 80
        except ValueError:
            port = 80
        path = "/" + after_scheme.split("/", 1)[1] if "/" in after_scheme else "/"
        return hostname, port, path

    async def _read_remaining_headers(self, reader: asyncio.StreamReader) -> list[bytes]:
        """Read remaining HTTP headers after the request line."""
        headers: list[bytes] = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break
            headers.append(line)
        return headers

    async def _handle_forward(
        self,
        request_line: str,
        headers: list[bytes],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: tuple,
    ):
        """Handle plain HTTP forward proxy requests."""
        parts = request_line.split()
        if len(parts) < 3:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        method, url, version = parts[0], parts[1], parts[2]
        parsed = self._parse_proxy_url(url)
        if parsed is None:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        hostname, port, path = parsed

        if not self._is_allowed(hostname):
            await self._reject_blocked(writer, method, hostname, port, peer)
            return

        result = await self._open_target(hostname, port)
        if result is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        target_reader, target_writer = result

        self.stats.allowed += 1
        logger.debug("ALLOWED: %s %s:%d from %s", method, hostname, port, peer)

        # Send the HTTP request to target and start bidirectional tunnel.
        target_writer.write(f"{method} {path} {version}\r\n".encode())
        for header in headers:
            # Proxy credentials authorize this hop only; never disclose them
            # to the destination server.
            name = header.split(b":", 1)[0].strip().lower()
            if name not in {b"proxy-authorization", b"proxy-connection"}:
                target_writer.write(header)
        target_writer.write(b"\r\n")
        await target_writer.drain()
        await self._tunnel(target_reader, target_writer, reader, writer)

    async def _tunnel(
        self,
        r1: asyncio.StreamReader,
        w1: asyncio.StreamWriter,
        r2: asyncio.StreamReader,
        w2: asyncio.StreamWriter,
    ):
        """Bidirectional data forwarding between two connections."""

        # No application-level idle timeout here: a CONNECT tunnel is legitimately
        # idle in one direction for the entire lifetime of a request. Claude Code
        # streams long model turns from api.anthropic.com over a single tunnel while
        # the client->server direction stays silent for minutes. A per-read timeout
        # that closes the *opposite* peer would tear down that live SSE stream mid
        # response, leaving the session "thinking" forever. TCP keepalive (enabled
        # on both sockets) reaps genuinely dead connections instead.
        async def forward(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError) as e:
                logger.debug("Tunnel forward error: %s", e)
            finally:
                try:
                    dst.close()
                except Exception as e:  # noqa: BLE001 — best-effort tunnel teardown; close failure only logged
                    logger.debug("Failed to close tunnel destination: %s", e)

        await asyncio.gather(
            forward(r1, w2),
            forward(r2, w1),
            return_exceptions=True,
        )

    async def _run_server(self):
        """Start the proxy server."""
        self._server = await asyncio.start_server(
            self._handle_client,
            self._host,
            self._requested_port,
        )
        addr = self._server.sockets[0].getsockname()
        self.port = addr[1]
        logger.debug(
            "Egress proxy started on %s:%d (allowlist: %s)",
            addr[0],
            self.port,
            ", ".join(sorted(self.allowlist)),
        )
        self._ready.set()

        async with self._server:
            await self._server.serve_forever()

    def _thread_target(self):
        """Thread entry point."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_server())
        except asyncio.CancelledError:
            pass
        finally:
            self._loop.close()

    def start(self) -> None:
        """Start the proxy in a background thread."""
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)
        if self.port == 0:
            raise RuntimeError("Egress proxy failed to start")

    def stop(self) -> None:
        """Stop the proxy."""
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=5)
        logger.debug(
            "Egress proxy stopped. Stats: %d allowed, %d blocked, %d errors",
            self.stats.allowed,
            self.stats.blocked,
            self.stats.errors,
        )
        if self.stats.blocked_log:
            logger.debug("Blocked connections: %s", self.stats.blocked_log[:20])
