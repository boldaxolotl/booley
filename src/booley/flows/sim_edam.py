"""Small Edalize simulation helpers used inside the Session Runtime.

The public simulation Flow executes only Verilator and Icarus.  Commercial
tool spellings remain recognizable here so configuration diagnostics can name
what a Target requested; they are rejected by :class:`SimulateFlow` before a
command is prepared.
"""

from __future__ import annotations

from pathlib import Path


def normalize_eda_tool(eda_tool: str | None) -> str:
    """Normalize a Target's ``flow_options.tool`` to an EDA family name."""
    if eda_tool:
        lowered = eda_tool.lower()
        if "icarus" in lowered or "iverilog" in lowered:
            return "icarus"
        if "xcelium" in lowered or "xrun" in lowered:
            return "xcelium"
        if "vcs" in lowered:
            return "vcs"
    return "verilator"


def sim_run_command(
    *,
    work_root: Path,
    work_dir: Path,
    toplevel: str,
    eda_tool: str,
    plusargs: list[str] | None = None,
) -> list[str]:
    """Return the command for an Edalize-built Verilator or Icarus model."""
    from . import edam as edam_layer

    rel = edam_layer.relpath_for_make(work_root, work_dir)
    plus = [pa if pa.startswith(("+", "-")) else f"+{pa}" for pa in (plusargs or [])]
    if eda_tool == "icarus":
        make_vars = {"EXTRA_OPTIONS": " ".join(plus)} if plus else None
        return edam_layer.make_command(rel, target="run", make_vars=make_vars)
    if eda_tool != "verilator":
        raise ValueError(
            f"simulator {eda_tool!r} is not supported; expected 'verilator' or 'icarus'"
        )
    return [f"{rel}/V{toplevel}", *plus]


def reemit_sim_summary(output: str, returncode: int) -> str:
    """Append Booley's structured verdict to raw simulator output."""
    from booley.sim.sim_result import (
        SIM_SUMMARY_PREFIX,
        count_sva_errors,
        extract_vrfc_warnings,
        format_summary,
        parse_sim_verdict,
    )

    if any(line.startswith(SIM_SUMMARY_PREFIX) for line in output.splitlines()):
        return output

    verdict = parse_sim_verdict(output)
    sva_errors = count_sva_errors(output)
    vrfc = extract_vrfc_warnings(output)
    if verdict is True:
        passed, inconclusive = sva_errors == 0, False
    elif verdict is False or returncode != 0 or sva_errors > 0:
        passed, inconclusive = False, False
    else:
        passed, inconclusive = False, True

    summary = format_summary(passed, sva_errors, vrfc, inconclusive=inconclusive)
    separator = "" if (not output or output.endswith("\n")) else "\n"
    return f"{output}{separator}{summary}"
