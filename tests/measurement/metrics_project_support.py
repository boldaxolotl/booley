"""Test-only Project fixture for ``metrics.toml`` tests: the Targets issue #3045's example names."""

from __future__ import annotations

from pathlib import Path

_SIM_CORE = (
    "CAPI=2:\nname: {vlnv}\ntargets:\n  {target}:\n    flow: sim\n"
    "    flow_options: {{tool: verilator}}\n    toplevel: top\n"
)

# The example ``metrics.toml`` of issue #3045, verbatim.
EXAMPLE_METRICS = """\
[defaults.sim]
tests_failed_max = 0

[defaults.coverage]
tests_failed_max = 0

[synth_core.synth]
area_kge_max = 60
"clk_i.fmax_mhz_min" = 400
"clk_i.wns_ns_min" = 0
track = ["cell_count"]
notes = { area_kge_max = "after DSP sharing" }

[synth_core.fpga]
lut_count_max = 5000
track = ["fmax_mhz"]

[sim_core.lint]
warnings_max = 0
errors_max = 0

[sim_core.sim]
"smoke.cycle_count_max" = 1000

[sim_core.coverage]
line_coverage_min = 90

[sim_fifo.sim]
off = ["tests_failed_max"]
track = ["tests_failed"]
"""


def write_metrics_project(root: Path, metrics: str | None = None) -> Path:
    """Write a Project with Targets ``synth_core``, ``sim_core``, ``sim_fifo`` and ``sim_two``.

    ``sim_core`` registers the Test ``smoke``, ``sim_two`` registers ``a`` and
    ``b``, ``sim_fifo`` and ``synth_core`` register none. ``shared`` is declared by
    two cores, so the bare name is ambiguous. Writes *metrics* as
    ``metrics.toml`` when given. Returns the Project root.
    """
    data = root / ".booley"
    data.mkdir(parents=True, exist_ok=True)
    (data / "booley.toml").write_bytes(b"[project]\n")
    (data / "tests.toml").write_bytes(
        b'[sim_core]\ntests = ["smoke"]\n[sim_two]\ntests = ["a", "b"]\n'
    )
    for name in ("synth_core", "sim_core", "sim_fifo", "sim_two"):
        text = _SIM_CORE.format(vlnv=f"::{name}:0", target=name)
        (root / f"{name}.core").write_bytes(text.encode())
    for vendor in ("a", "b"):
        text = _SIM_CORE.format(vlnv=f"vendor:{vendor}:x:1.0", target="shared")
        (root / f"shared_{vendor}.core").write_bytes(text.encode())
    if metrics is not None:
        (data / "metrics.toml").write_bytes(metrics.encode())
    return root
