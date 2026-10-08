"""Neutral captured process identity: values and wire validation, without process I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str

_IDENTITY_KINDS = {
    "linux-procfs-start",
    "posix-ps-start",
    "windows-creation-time",
}


@dataclass(frozen=True)
class ProcessIdentity:
    """PID plus platform identity fields that survive argv changes."""

    pid: int
    identity_scope: str
    start_token: int
    identity_kind: str = "linux-procfs-start"

    @property
    def pid_namespace(self) -> str:
        """Compatibility name for callers displaying the identity scope."""
        return self.identity_scope

    @property
    def start_ticks(self) -> int:
        """Compatibility name for callers comparing the identity token."""
        return self.start_token

    def to_payload(self) -> dict[str, Any]:
        """Serialize this identity for a durable protocol record."""
        return {
            "pid": self.pid,
            "identity_kind": self.identity_kind,
            "identity_scope": self.identity_scope,
            "start_token": self.start_token,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProcessIdentity | None:
        """Parse one untrusted protocol identity, or return ``None``."""
        try:
            data = require_dict(payload, field="process identity")
            scope_key = "identity_scope" if "identity_scope" in data else "pid_namespace"
            token_key = "start_token" if "start_token" in data else "start_ticks"
            identity_scope = require_str(data, scope_key)
            identity_kind = data.get("identity_kind")
            if identity_kind is None:
                if identity_scope == "windows":
                    identity_kind = "windows-creation-time"
                elif identity_scope.startswith("ps:"):
                    identity_kind = "posix-ps-start"
                else:
                    identity_kind = "linux-procfs-start"
            kind = require_str({"identity_kind": identity_kind}, "identity_kind")
            if kind not in _IDENTITY_KINDS:
                raise BoundaryError(f"unsupported process identity kind {kind}")
            if kind == "windows-creation-time" and identity_scope != "windows":
                raise BoundaryError("Windows process identity has an invalid scope")
            if kind == "posix-ps-start" and not identity_scope.startswith("ps:"):
                raise BoundaryError("POSIX process identity has an invalid scope")
            return cls(
                pid=require_int(data.get("pid"), field="process identity pid"),
                identity_scope=identity_scope,
                start_token=require_int(data.get(token_key), field="process identity start token"),
                identity_kind=kind,
            )
        except BoundaryError:
            return None
