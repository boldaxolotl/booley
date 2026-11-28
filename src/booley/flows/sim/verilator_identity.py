"""Exact producing compiler identity of the coverage collector."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VerilatorCollectorIdentity:
    """Exact stable Verilator tag and full upstream commit identity."""

    tag: str
    commit: str


PINNED_VERILATOR = VerilatorCollectorIdentity(
    tag="v5.054",
    commit="bbb60a403932a9f695cb6ff001ceb73de6ad9234",
)
