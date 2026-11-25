"""target_naming.py — the Target naming convention: ``<axis>_<subject>``.

A **Target** name is the only handle a human or an agent gets on a design-description: it
appears in ``--target``, in Doctor metadata, as the ``{target}`` suffix of
per-target Criteria (``sim_pass_<target>``), and as a ``tests.toml`` section key.
The convention makes it say which Booley Flow drives it, up front:

    <axis>_<subject>[_<variant>…]        sim_soc, synth_matmul_par_b8

The **axis** is one of four fixed tokens (:data:`TARGET_AXES`) naming the Booley Flow
family, not the ``flow:`` field. Three reasons the axis is *not* derivable from
the ``.core`` metadata, so the name has to carry it:

  * CAPI2 has no synth flow — a Yosys Target and a Vivado Target are both
    ``flow: generic``, so ``synth`` vs ``fpga`` exists nowhere but the name.
  * A project may declare a synth Target as ``flow: lint`` purely as a
    resolution vehicle (Booley rebuilds the real EDAM itself), which makes the
    declared flow actively misleading.
  * Using the canonical Booley Flow name makes the Target obvious at every call site.

Prefix, not suffix, because ``booley targets`` sorts entries by name within a
core group (:mod:`booley.targets.target_surface`) — a leading axis renders the listing
pre-grouped by Flow, and makes ``--target sim_<TAB>`` a useful completion.

A bare axis token (``sim``) still maps to its Flow (:func:`axis_of`), so Flow
intent and Criteria selection keep working.

This module maps a Target name to its axis and FPGA intent.
"""

from __future__ import annotations

# The axis vocabulary: leading token -> the target-aware Booley Flows it covers.
TARGET_AXES: dict[str, tuple[str, ...]] = {
    "sim": ("sim",),
    "lint": ("lint",),
    "synth": ("synth",),
    "fpga": ("fpga",),
}

# Inverted: the axis a given Booley Flow's Targets should carry.
AXIS_FOR_FLOW: dict[str, str] = {
    flow: axis for axis, flows in TARGET_AXES.items() for flow in flows
}


def axis_of(name: str) -> str | None:
    """The axis *name* declares, or ``None`` when it declares none.

    A bare axis token (``lint``) still names its axis, because Flow intent and
    Criteria selection depend on it.
    """
    for axis in TARGET_AXES:
        if name == axis or name.startswith(f"{axis}_"):
            return axis
    return None


def fpga_intent(name: str, eda_tool: str | None) -> bool:
    """Whether a Target declares FPGA implementation intent.

    A recognized Booley axis is authoritative because the FPGA Flow rebuilds
    the resolved design description into its own Vivado EDAM. Unprefixed
    Targets fall back to the declared EDA tool so vendored upstream Vivado
    Targets remain usable without being renamed.
    """
    axis = axis_of(name)
    if axis is not None:
        return axis == "fpga"
    return eda_tool == "vivado"
