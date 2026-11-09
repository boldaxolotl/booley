"""Host-private Session store policy: the repair text, plus the `PrivateStore` re-export."""

from booley.core.private_store import (
    PrivateStore,
)

# Repair offered when a Session store file is not named for the Project Root it
# holds; `{path}` and `{root}` are filled in by `booley.core.private_store`.
SESSION_STORE_REPAIR = (
    "remove {path}, then on the host run `booley init --seed --project {root}` "
    "to reissue its Sandbox spec"
)

__all__ = ["SESSION_STORE_REPAIR", "PrivateStore"]
