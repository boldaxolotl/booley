"""Private work description shared by Simulation execution and its adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AdapterKind = Literal["verilator", "icarus", "cocotb"]


@dataclass(frozen=True)
class PreparedSimulationWork:
    """Resolved facts needed by exactly one adapter invocation."""

    adapter: AdapterKind
    build_dir: str
    run_cwd: str
    timeout_s: int
    eda_tool: str = ""
    max_rundir_bytes: int = 0
    plusargs: tuple[str, ...] = ()
    trace: bool = False
    trace_mode: str = "vcd_fifo"
    trace_scope: str = ""
    trace_args: tuple[str, ...] = ()
    trace_files: tuple[str, ...] = ()
    pass_sentinels: tuple[str, ...] = ()
    fail_sentinels: tuple[str, ...] = ()
    top: str = ""
    cocotb_module: str = ""
    tests: tuple[str, ...] = ()
    result_verbosity: str = "compact"
    sim_time_grace_s: float = 0.0
    adapter_result_path: str = ""
    attempt_token: str = ""
    target_identity: str = ""

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("Simulation adapter timeout must be positive")
        transport = (self.adapter_result_path, self.attempt_token, self.target_identity)
        if any(transport) and not all(transport):
            raise ValueError(
                "Simulation adapter transport requires a result path, attempt token, "
                "and Target identity"
            )


__all__ = ["AdapterKind", "PreparedSimulationWork"]
