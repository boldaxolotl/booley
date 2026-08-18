"""Interface schema model — the DUT's post-ticket interface contract.

Pydantic models describing a design's parameters and ports, carried on
``dut_info.interface`` as an explicit contract (rather than re-derived
from RTL source). Split out of ``development_state`` (principle 8): this module
owns only the interface-spec schema and its parsing/validation, and imports
nothing from ``development_state`` so the dependency flows one way.
"""

from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_VALID_PORT_DIRS: frozenset[str] = frozenset({"input", "output", "inout"})
_VALID_PORT_SOURCES: frozenset[str] = frozenset({"combinational", "registered"})
_VALID_PORT_CLOCKINGS: frozenset[str] = frozenset({"synchronous", "asynchronous"})


class ParameterSpec(BaseModel):
    """One parameter from the dut_info interface declaration."""

    model_config = ConfigDict(extra="ignore")

    name: str
    default: str = ""

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("parameter name must be non-empty")
        return v


class BitFieldSpec(BaseModel):
    """Named meaning for one bit or bit range within a packed port."""

    model_config = ConfigDict(extra="ignore")

    bits: str
    name: str | None = None
    active: str | None = None
    meaning: str | None = None
    drives_state: str | None = None
    values: dict[str, str] | None = None
    required_value: str | None = None

    @field_validator("bits")
    @classmethod
    def _bits_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("bit_field.bits must be non-empty")
        return v


class PortSpec(BaseModel):
    """One port from the dut_info interface declaration.

    `width` accepts either a literal integer (e.g. ``8``) or a string naming a
    parameter that sets the width (e.g. ``"DBITS"``).  Output ports declare
    `source`: ``"registered"`` means the pad is driven directly by a flop Q;
    ``"combinational"`` means anything else, including logic after a flop.
    Input ports do not declare a source.  ``pipelined(N)``
    is intentionally rejected — it conflates clock domain with stage count.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    dir: str
    width: int | str
    source: str | None = None
    semantics: str = ""
    role: str | None = None
    clocking: str | None = None
    synchronous_to: str | None = None
    bit_fields: list[BitFieldSpec] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_timing(cls, data: Any) -> Any:
        """Map old output `timing` to `source`; ignore legacy input timing."""
        if not isinstance(data, dict):
            return data
        norm = dict(data)
        timing = norm.pop("timing", None)
        if norm.get("source") is None and norm.get("dir") == "output" and timing is not None:
            norm["source"] = timing
        return norm

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("port name must be non-empty")
        return v

    @field_validator("dir")
    @classmethod
    def _dir_valid(cls, v: str) -> str:
        if v not in _VALID_PORT_DIRS:
            raise ValueError(
                f"port.dir={v!r} not in {sorted(_VALID_PORT_DIRS)}",
            )
        return v

    @field_validator("source")
    @classmethod
    def _source_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in _VALID_PORT_SOURCES:
            raise ValueError(
                f"port.source={v!r} not in {sorted(_VALID_PORT_SOURCES)} "
                "(pipelined(N) is not accepted — declare each pipeline stage's "
                "observable output as 'registered')",
            )
        return v

    @field_validator("clocking")
    @classmethod
    def _clocking_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in _VALID_PORT_CLOCKINGS:
            raise ValueError(
                f"port.clocking={v!r} not in {sorted(_VALID_PORT_CLOCKINGS)}",
            )
        return v

    @field_validator("synchronous_to")
    @classmethod
    def _synchronous_to_nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("port.synchronous_to must be non-empty when present")
        return v

    @field_validator("semantics")
    @classmethod
    def _semantics_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "port.semantics must be non-empty — one sentence describing "
                "what this port carries / asserts / triggers",
            )
        return v

    @model_validator(mode="after")
    def _source_only_on_outputs(self) -> PortSpec:
        if self.dir == "output":
            if self.source is None:
                raise ValueError("output port.source is required")
        elif self.source is not None:
            raise ValueError("port.source is only valid for output ports")
        if self.clocking == "synchronous" and self.synchronous_to is None:
            raise ValueError("synchronous port.synchronous_to is required")
        if self.clocking != "synchronous" and self.synchronous_to is not None:
            raise ValueError("port.synchronous_to is only valid for synchronous ports")
        return self


class InterfaceSpec(BaseModel):
    """Post-ticket interface contract for the DUT.

    Carried on ``dut_info.interface`` so TB-side work can consume an explicit
    declaration as a contract rather than re-deriving it from RTL source
    (which the RTL-blind ``tb_coder`` cannot see for this-ticket greenfield
    code).
    """

    model_config = ConfigDict(extra="ignore")

    parameters: list[ParameterSpec] = Field(default_factory=list)
    ports: list[PortSpec] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.parameters or self.ports)
