"""Give VS Code Live Preview a fresh port on every container attach.

Live Preview defaults to remote ports 3000/3001. VS Code's long-lived local
process can retain a tunnel for those ports after the remote container that
owned it has gone away. A later container then receives the dead tunnel from
``vscode.env.asExternalUri`` and its embedded preview stays blank even though
the new in-container HTTP server is healthy.

The generated devcontainer runs this module from ``postAttachCommand`` before
Live Preview is activated. It replaces the seeded ``livePreview.portNumber``
in the remote Machine settings with a random, currently free pair from the
IANA dynamic/private range. Consequently each attached runtime asks VS Code
for a new tunnel instead of inheriting a dead one from another container.

The edit is narrow, atomic, and best-effort: malformed or missing settings
leave the file untouched and never fail the attach hook.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

_SETTINGS_RELATIVE_PATH = Path(".vscode-server/data/Machine/settings.json")
_PORT_PATTERN = re.compile(r'("livePreview\.portNumber"\s*:\s*)(\d+)')
_MIN_PORT = 49152
_MAX_START_PORT = 65534  # Live Preview reserves the next port for WebSocket.
_PORT_ATTEMPTS = 64
_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.1


def _agent_home() -> Path:
    return Path(os.environ.get("HOME", "/home/agent"))


def _port_pair_is_free(port: int) -> bool:
    """Whether loopback TCP ports *port* and *port + 1* can both be bound."""
    sockets: list[socket.socket] = []
    try:
        for candidate in (port, port + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(sock)
            sock.bind(("127.0.0.1", candidate))
        return True
    except OSError:
        return False
    finally:
        for sock in sockets:
            sock.close()


def choose_port(randbelow: Callable[[int], int] = secrets.randbelow) -> int | None:
    """Choose a free Live Preview HTTP/WebSocket port pair, if one is found."""
    width = _MAX_START_PORT - _MIN_PORT + 1
    for _ in range(_PORT_ATTEMPTS):
        port = _MIN_PORT + randbelow(width)
        if _port_pair_is_free(port):
            return port
    return None


def patch_settings(path: Path, port: int) -> bool:
    """Atomically replace Live Preview's seeded port in *path*.

    A regex replacement deliberately preserves VS Code's formatting and any
    JSON-with-comments content instead of reserializing the whole user-owned
    Machine settings document.
    """
    try:
        source = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode
    except OSError:
        return False
    updated, replacements = _PORT_PATTERN.subn(rf"\g<1>{port}", source)
    if replacements != 1:
        return False

    temporary = path.with_name(f".{path.name}.booley-{os.getpid()}")
    try:
        temporary.write_text(updated, encoding="utf-8")
        temporary.chmod(mode)
        temporary.replace(path)
    except OSError:
        with suppress(OSError):
            temporary.unlink()
        return False
    return True


def _wait_for_seeded_settings(
    path: Path,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    """Wait briefly for devcontainers to finish writing the Machine setting."""
    deadline = clock() + _WAIT_SECONDS
    while clock() < deadline:
        try:
            if _PORT_PATTERN.search(path.read_text(encoding="utf-8")):
                return True
        except OSError:
            pass
        sleep(_POLL_SECONDS)
    return False


def main(
    argv: Sequence[str] = (),
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Randomize the remote Live Preview port; never fail the attach hook."""
    import argparse

    try:
        argparse.ArgumentParser(
            prog="python -m booley.runtime.incontainer_live_preview",
            description="Assign Live Preview a fresh remote port for this container attach.",
        ).parse_args(argv)
    except SystemExit as exc:
        if exc.code:
            print("live-preview: bad arguments; settings unchanged", file=sys.stderr)
        return 0

    path = _agent_home() / _SETTINGS_RELATIVE_PATH
    if not _wait_for_seeded_settings(path, sleep=sleep, clock=clock):
        print("live-preview: seeded Machine setting not found; settings unchanged")
        return 0
    port = choose_port()
    if port is None or not patch_settings(path, port):
        print("live-preview: could not assign a fresh port; settings unchanged")
        return 0
    print(f"live-preview: assigned fresh remote ports {port}/{port + 1}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
