"""Validated identity and visibility scope for endpoint presentation events."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from booley.core.boundary import BoundaryError, require_str


class DisplayScope(StrEnum):
    """Whether an endpoint is visible in the Developer Agent's display."""

    DEVELOPER = "developer"
    NESTED = "nested"

    @classmethod
    def parse(cls, value: object) -> DisplayScope:
        """Validate one scope read from persisted or external data."""
        raw = require_str({"display_scope": value}, "display_scope")
        try:
            return cls(raw)
        except ValueError as exc:
            raise BoundaryError(f"unsupported display scope {raw}") from exc

    @classmethod
    def current(cls) -> DisplayScope:
        """Resolve the current process's presentation scope."""
        if os.environ.get("BOOLEY_NESTED_AGENT") == "1":
            return cls.NESTED
        return cls.DEVELOPER


@dataclass(frozen=True)
class DisplayIdentity:
    """One invocation's stable identifier and presentation scope."""

    invocation_id: str
    scope: DisplayScope

    def __post_init__(self) -> None:
        require_str({"invocation_id": self.invocation_id}, "invocation_id")
        if not isinstance(self.scope, DisplayScope):
            object.__setattr__(self, "scope", DisplayScope.parse(self.scope))

    @classmethod
    def current(cls, invocation_id: str) -> DisplayIdentity:
        """Build identity for a locally dispatched invocation."""
        return cls(invocation_id=invocation_id, scope=DisplayScope.current())

    @classmethod
    def from_event(cls, event: Mapping[str, Any]) -> DisplayIdentity | None:
        """Parse an event identity, preserving only fully legacy events.

        Old events contain neither identity field. A partial identity is corrupt
        external input and must not fall through to legacy nesting semantics.
        """
        has_id = "invocation_id" in event
        has_scope = "display_scope" in event
        if not has_id and not has_scope:
            return None
        if not has_id or not has_scope:
            raise BoundaryError(
                "display event identity must include invocation_id and display_scope"
            )
        return cls(
            invocation_id=require_str(event, "invocation_id"),
            scope=DisplayScope.parse(event.get("display_scope")),
        )

    def apply(self, event: dict[str, Any]) -> dict[str, Any]:
        """Add this identity's serialized fields to an event."""
        event["invocation_id"] = self.invocation_id
        event["display_scope"] = self.scope.value
        return event
