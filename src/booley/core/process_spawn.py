"""Session creation shared by low-level lease guardians and Runtime groups."""

from __future__ import annotations

from typing import TypedDict


class SessionSpawnOptions(TypedDict):
    """POSIX session option accepted by synchronous and asynchronous spawns."""

    start_new_session: bool


def new_session_kwargs() -> SessionSpawnOptions:
    """Return POSIX spawn options that establish a known child session identity."""
    return {"start_new_session": True}
