"""Transport-observed loopback socket identity, never trusted request headers."""

from __future__ import annotations

import ipaddress
import socket
from itertools import islice
from pathlib import Path
from typing import Any

from starlette.requests import Request

from booley.runtime.pid import ProcessIdentity, capture_process_identity

RESERVED_HEADERS = frozenset({b"x-booley-peer-host", b"x-booley-peer-port"})


def _endpoint(host: str, port: int) -> str:
    address = ipaddress.ip_address(host)
    if address.version != 4:
        raise ValueError("IPv6 peer lookup unavailable")
    return f"{socket.inet_aton(host)[::-1].hex().upper()}:{port:04X}"


def _inodes(proc_root: Path, client: tuple[str, int], server: tuple[str, int]) -> set[str]:
    local, remote = _endpoint(*client), _endpoint(*server)
    lines = (proc_root / "net/tcp").read_text(encoding="ascii").splitlines()
    return {
        fields[9]
        for line in lines[1:65537]
        if len(fields := line.split()) > 9
        and fields[1] == local
        and fields[2] == remote
        and fields[3] == "01"
    }


def peer_identity(
    client: tuple[str, int], server: tuple[str, int], *, proc_root: Path = Path("/proc")
) -> ProcessIdentity | None:
    """Unique endpoint/inode owner, revalidated against both PID reuse and socket races."""
    try:
        if not ipaddress.ip_address(client[0]).is_loopback:
            return None
        inodes = _inodes(proc_root, client, server)
        if len(inodes) != 1:
            return None
        target = f"socket:[{next(iter(inodes))}]"
        owners: list[tuple[ProcessIdentity, Path]] = []
        for directory in islice(proc_root.iterdir(), 8192):
            if not directory.name.isdecimal():
                continue
            try:
                matching = [
                    fd
                    for fd in islice((directory / "fd").iterdir(), 4096)
                    if str(fd.readlink()) == target
                ]
            except OSError:
                continue
            if not matching:
                continue
            identity = capture_process_identity(int(directory.name), proc_root=proc_root)
            if identity is not None:
                owners.append((identity, matching[0]))
        if len(owners) != 1:
            return None
        identity, fd = owners[0]
        if capture_process_identity(identity.pid, proc_root=proc_root) != identity:
            return None
        valid = str(fd.readlink()) == target and _inodes(proc_root, client, server) == inodes
        return identity if valid else None
    except (OSError, ValueError, UnicodeError):
        return None


def observed_peer(
    request: Request | None, *, proc_root: Path = Path("/proc")
) -> ProcessIdentity | None:
    """Read only this SDK message's transport-stamped request, never session context."""
    if request is None:
        return None
    host = request.headers.get("x-booley-peer-host")
    port = request.headers.get("x-booley-peer-port")
    server = request.scope.get("server")
    if not host or not port or not server:
        return None
    try:
        return peer_identity((host, int(port)), tuple(server), proc_root=proc_root)
    except ValueError:
        return None


class PeerBoundary:
    """Strip duplicate/spoofed reserved headers before stamping ASGI observations."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        headers = [
            (key, value)
            for key, value in scope.get("headers", [])
            if key.lower() not in RESERVED_HEADERS
        ]
        client, server = scope.get("client"), scope.get("server")
        try:
            if client and server and ipaddress.ip_address(client[0]).is_loopback:
                headers.extend(
                    [
                        (b"x-booley-peer-host", client[0].encode()),
                        (b"x-booley-peer-port", str(client[1]).encode()),
                    ]
                )
        except ValueError:
            pass  # Unsupported addresses degrade attribution only.
        scope["headers"] = headers
        await self.app(scope, receive, send)
