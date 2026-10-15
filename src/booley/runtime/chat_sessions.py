"""Process-owned display identity for Codex tabs launched without a shared daemon."""

from __future__ import annotations

from pathlib import Path

from booley.runtime.handles import HandleStore, session_writer
from booley.runtime.pid import ProcessIdentity, capture_process_identity
from booley.runtime.session_activity import process_session_key

MAX_ANCESTORS = 32


def _codex_process(proc_root: Path, pid: int) -> tuple[str, bool]:
    """Recognize a CLI boundary, excluding its app-server helper processes."""
    executable = Path(
        str((proc_root / str(pid) / "exe").readlink()).removesuffix(" (deleted)")
    ).name
    if executable != "codex":
        return executable, False
    with (proc_root / str(pid) / "cmdline").open("rb") as stream:
        argv = stream.read(4096).split(b"\0")
    return executable, argv[1:2] != [b"app-server"]


def reserve_codex_chat(booley_dir: Path, identity: ProcessIdentity) -> str:
    """Reserve the tab's handle and mark this process as a Booley Codex launch."""
    key = process_session_key(identity)
    with session_writer(booley_dir):
        store = HandleStore(booley_dir)
        handles = store.load()
        label = handles.reserve(key, "S", predecessors=("chat:codex:" + key,))
        store.save(handles)
        return label


def codex_chat_owner(
    booley_dir: Path, peer: ProcessIdentity, *, proc_root: Path = Path("/proc")
) -> ProcessIdentity | None:
    """Find a marked launch ancestor; ordinary Codex/Claude ancestors cannot match."""
    handles = HandleStore(booley_dir).load()
    current = peer
    nearest_client = child = None
    for _ in range(MAX_ANCESTORS):
        if capture_process_identity(current.pid, proc_root=proc_root) != current:
            return None
        try:
            executable, client = _codex_process(proc_root, current.pid)
            if client and nearest_client is None:
                nearest_client = current.pid
            key = process_session_key(current)
            if handles.aliases.get("chat:codex:" + key) == key:
                expected = child if executable in {"node", "nodejs"} else current.pid
                return current if nearest_client == expected else None
            stat = (proc_root / str(current.pid) / "stat").read_text(encoding="utf-8")
            parent_pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError, UnicodeError):
            return None
        if parent_pid <= 1 or parent_pid == current.pid:
            return None
        parent = capture_process_identity(parent_pid, proc_root=proc_root)
        if parent is None or parent.identity_scope != peer.identity_scope:
            return None
        child, current = current.pid, parent
    return None
