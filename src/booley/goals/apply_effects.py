"""Captured lifecycle effects: compare before/after bytes, never overwrite drift."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_str_value
from booley.goals.proposals import ProposalError, text
from booley.runtime.atomic_files import atomic_replace_bytes, durable_unlink


@dataclass(frozen=True)
class FileEffect:
    """An exact destination and its captured pre/post images."""

    path: Path
    before: bytes | None
    after: bytes | None
    label: str

    def to_json(self) -> dict[str, Any]:
        """Persist complete bytes, not references to mutable input files."""
        return {
            "path": str(self.path),
            "before": _encoded(self.before),
            "after": _encoded(self.after),
            "label": self.label,
        }

    @classmethod
    def from_json(cls, value: object) -> FileEffect:
        """Validate transaction effects at the filesystem boundary."""
        raw = require_dict(value, field="effect")
        if set(raw) != {"path", "before", "after", "label"}:
            raise ProposalError("invalid lifecycle effect fields")
        path = Path(text(raw["path"], "effect.path"))
        require_destination(path, text(raw["label"], "effect.label"))
        return cls(
            path,
            _decoded(raw["before"]),
            _decoded(raw["after"]),
            text(raw["label"], "effect.label"),
        )

    def require_expected(self) -> None:
        """Refuse unexpected external bytes or symlink ancestors without mutation."""
        if any(path.is_symlink() for path in (self.path, *self.path.parents)):
            raise ProposalError(f"recovery conflict: symlink destination {self.path}")
        if read_bytes(self.path) not in (self.before, self.after):
            raise ProposalError(
                f"recovery conflict at {self.path}: expected captured before/after bytes; restore those bytes before retrying"
            )

    def apply(self) -> None:
        """An idempotent atomic replacement; caller owns lifecycle/storage locks."""
        self.require_expected()
        if read_bytes(self.path) == self.after:
            return
        if self.after is None:
            durable_unlink(self.path)
        else:
            atomic_replace_bytes(self.path, self.after, mode=0o644)


def read_bytes(path: Path) -> bytes | None:
    """Missing is a captured state, distinct from an empty file."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def require_destination(path: Path, label: str) -> None:
    """Reject lexical traversal and labels without lifecycle authority."""
    if not path.is_absolute() or ".." in path.parts:
        raise ProposalError("lifecycle destinations must be absolute without traversal")
    if label not in {"record", "state", "ledger", "waiver", "candidate"}:
        raise ProposalError("unknown lifecycle effect label")


def _encoded(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def _decoded(value: object) -> bytes | None:
    return (
        None
        if value is None
        else base64.b64decode(require_str_value(value, field="effect bytes"), validate=True)
    )
