"""Exact producing compiler identity shared by coverage and declarations."""

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
