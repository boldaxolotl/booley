"""How the Dashboard names a session: its mode and the readable form of its key."""

from __future__ import annotations

import re

from booley.runtime.session_activity import process_identity_from_session_key

#: The mode of every connected session, as the Run review names it.
SESSION_MODE = "Interactive"

#: Linux names a PID namespace link target ``pid:[<inode>]``.
_PID_NAMESPACE = re.compile(r"pid:\[(\d+)\]")


def session_key_label(key: str) -> str:
    """Readable form of a session key for display; the key itself stays the durable ID.

    A process key names its client by PID (and, on Linux, the PID namespace
    inode); any other key, and a malformed process key, is shown unchanged.
    """
    identity = process_identity_from_session_key(key)
    if identity is None:
        return key
    if identity.identity_kind != "linux-procfs-start":
        return f"PID {identity.pid}"
    inode = _PID_NAMESPACE.fullmatch(identity.identity_scope)
    return f"PID {identity.pid} · ns {inode[1] if inode else identity.identity_scope}"
