"""Boundary errors shared by Goal proposals and their neutral binding values."""

from pathlib import Path


class ProposalError(ValueError):
    """A saved request or decision is invalid or no longer applicable."""

    def __init__(self, message: str = "", *, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path
