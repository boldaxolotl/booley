"""Host-private Session store policy: the repair text, plus the `PrivateStore` re-export."""

from pathlib import Path

from booley.core.host_shell import quote_arg
from booley.core.private_store import (
    PrivateStore,
)


def session_store_repair(path: Path, root: str) -> str:
    """Repair offered when a Session store file is not named for the Project Root it holds."""
    return (
        f"remove {path}, then on the host run `booley init --seed --project {quote_arg(root)}` "
        "to reissue its Sandbox spec"
    )


__all__ = ["PrivateStore", "session_store_repair"]
