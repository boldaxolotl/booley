"""Exact producing compiler identity of the coverage collector."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VerilatorCollectorIdentity:
    """Exact stable Verilator tag and full upstream commit identity."""

    tag: str
    commit: str


PINNED_VERILATOR = VerilatorCollectorIdentity(
    tag="v5.052",
    commit="ea338be98e1e838d3518809ce8899f85a009963c",
)
