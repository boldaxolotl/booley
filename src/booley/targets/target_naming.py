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
intent and Criteria selection keep working, but it is *not* conventional: with
no subject it says nothing about which design it drives.

This module states the rule and nothing else: :mod:`booley.harness.doctor` is
where a violation becomes a (non-fatal) finding, and only for Booley-authored
cores — a vendored upstream ``.core`` must stay byte-identical to upstream, so
its Target names are not the project's to rename (ADR 0036).
"""

from __future__ import annotations

import re

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

# Legacy *trailing* axis words seen in pre-convention projects, mapped to the
# axis they meant. Used only to propose a rename — ``matmul_par_b8_synth``
# carries enough to suggest ``synth_matmul_par_b8`` rather than shrug.
_LEGACY_AXIS_SUFFIXES: dict[str, str] = {
    "sim": "sim",
    "lint": "lint",
    "synth": "synth",
    "synthesis": "synth",
    "asic": "synth",
    "fpga": "fpga",
    "impl": "fpga",
}

# A conformant name: lowercase alphanumerics in underscore-separated segments.
# Deliberately no uppercase and no dashes — the name is pasted into shell
# completions, TOML keys, and Criterion identifiers alike.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

# FuseSoC's implicit resolution fallback. Never a Booley Target, so it is exempt
# from the axis rule (and carries its own doctor finding).
FUSESOC_RESERVED: frozenset[str] = frozenset({"default"})


def axis_of(name: str) -> str | None:
    """The axis *name* declares, or ``None`` when it declares none.

    A bare axis token (``lint``) still names its axis, because Flow intent and
    Criteria selection depend on it; it just breaks the naming convention
    (see :func:`is_conventional`).
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


def is_conventional(name: str) -> bool:
    """True when *name* satisfies the convention (or is FuseSoC-reserved)."""
    if name in FUSESOC_RESERVED:
        return True
    return name not in TARGET_AXES and axis_of(name) is not None and bool(_NAME_RE.match(name))


def violation(name: str) -> str | None:
    """Why *name* breaks the convention, or ``None`` when it conforms.

    One short clause, phrased to be pasted straight into a doctor warning.
    """
    if is_conventional(name):
        return None
    if not _NAME_RE.match(name):
        return "not lowercase_snake_case"
    if name in TARGET_AXES:
        return "bare axis; add a subject"
    return f"no axis prefix ({'/'.join(TARGET_AXES)})"


def strip_legacy_axis(name: str) -> tuple[str, str | None]:
    """Split *name* into its subject and the axis its legacy suffix implied.

    ``('matmul_par_b8', 'synth')`` for ``matmul_par_b8_synth``; the axis is
    ``None`` when no trailing axis word is present. A name that is *only* a
    legacy axis word (``synth``) yields an empty subject.
    """
    for suffix, axis in _LEGACY_AXIS_SUFFIXES.items():
        if name == suffix:
            return "", axis
        if name.endswith(f"_{suffix}"):
            return name[: -len(suffix) - 1], axis
    return name, None


def _strip_leading_legacy_axis(subject: str, axis: str) -> str:
    """Drop a redundant *leading* legacy axis word from *subject*.

    ``suggest_name`` prepends the real axis, so a subject that already opens
    with a word meaning that same axis produces a stutter (``asic_core`` under
    the ``synth`` axis → ``synth_asic_core``). Strip the leading word only when
    it maps to *this* axis, leaving ``synth_core``. A word meaning a different
    axis (``sim_core`` under ``synth``) is a real subject token and kept.
    """
    for word, word_axis in _LEGACY_AXIS_SUFFIXES.items():
        if word_axis != axis:
            continue
        if subject == word:
            return ""
        if subject.startswith(f"{word}_"):
            return subject[len(word) + 1 :]
    return subject


def _sanitize_subject(raw: str) -> str:
    """Lowercase *raw* and fold anything outside ``[a-z0-9_]`` into ``_``."""
    return re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")


def suggest_name(name: str, axis: str | None = None, *, subject: str | None = None) -> str | None:
    """A conformant rename for *name*, or ``None`` when none can be inferred.

    *axis* pins the answer when the caller knows it (from Doctor metadata,
    say); otherwise the trailing legacy axis word decides. Returns
    ``None`` rather than guessing when neither source names an axis — a bad
    suggestion costs more than no suggestion.

    *subject* is the fallback when *name* itself carries none (a bare axis such
    as ``sim``): typically the core name, giving ``sim_<core name>``. It is only
    sanitized, never legacy-stripped, so the suggestion stays literally
    ``<axis>_<subject>``. A bare axis with no fallback subject yields ``None``.
    """
    name_subject, legacy_axis = strip_legacy_axis(name)
    axis = axis or legacy_axis
    if axis not in TARGET_AXES:
        return None
    name_subject = _strip_leading_legacy_axis(_sanitize_subject(name_subject), axis)
    if name_subject:
        return f"{axis}_{name_subject}"
    fallback = _sanitize_subject(subject) if subject else ""
    return f"{axis}_{fallback}" if fallback else None
