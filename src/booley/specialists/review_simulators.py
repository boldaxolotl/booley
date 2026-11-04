"""Tell a testbench Reviewer which simulator runs each scoped file.

The review guides carry rules that exist only because of one simulator
(``[Icarus only]``, ``[Vivado xsim only]``). Without facts the Reviewer guesses
the simulator and applies those rules to the wrong one, so this module resolves
the simulators from the Project's simulation Targets and renders them as the
``## Simulation Context`` prompt section.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.fusesoc.fusesoc_registry import canonical_project_path
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle, flow_can_drive

from .review_contract import resolve_review_scope

logger = logging.getLogger(__name__)

# EDA tool names as authored in ``.core`` files -> the label guides use in their
# ``[<Simulator> only]`` marks. Unlisted tools keep their declared name.
_SIMULATOR_LABELS = {
    "icarus": "Icarus",
    "iverilog": "Icarus",
    "verilator": "Verilator",
    "xsim": "Vivado xsim",
}

_NO_TARGET_REASON = "no simulation Target uses this file"
_UNRESOLVED_REASON = "the Project's Targets could not be resolved"


@dataclass(frozen=True)
class ScopeSimulators:
    """Simulators resolved for each scoped file of one review."""

    # (scoped path in caller spelling, sorted simulator labels); no labels = unknown.
    files: tuple[tuple[str, tuple[str, ...]], ...]
    # The Target catalog itself could not be built or listed.
    unresolved: bool = False
    # Identities of simulation Targets whose inputs could not be inspected.
    skipped: tuple[str, ...] = ()


def _label(eda_tool: str | None) -> str | None:
    if not eda_tool:
        return None
    return _SIMULATOR_LABELS.get(eda_tool.lower(), eda_tool)


def _is_simulation_target(handle: TargetHandle) -> bool:
    return handle.flow == "sim" or flow_can_drive("sim", handle)


def _normalize(project_root: Path, path: str) -> str:
    """Spell *path* the way ``canonical_project_path`` spells Target inputs."""
    return canonical_project_path(project_root, path.replace("\\", "/").removeprefix("./"))


def resolve_scope_simulators(project_root: Path, scope: Sequence[str]) -> ScopeSimulators:
    """Map each scoped source file to the simulators of the simulation Targets using it.

    Never raises: a simulator probe must not block a review. Failures degrade to
    unknown simulators, which the rendered section reports honestly.
    """
    if not scope:
        return ScopeSimulators(files=())
    contract = resolve_review_scope(list(scope), category="tb", allow_support=True)
    paths = contract.simulation_sources
    if not paths:
        return ScopeSimulators(files=())
    unknown = tuple((path, ()) for path in paths)
    try:
        catalog = TargetCatalog.build(project_root)
        handles = [handle for handle in catalog.list() if _is_simulation_target(handle)]
    except Exception as exc:  # noqa: BLE001 — best-effort probe; never block a review on it
        logger.warning("Simulator resolution for the review scope failed: %s", exc)
        return ScopeSimulators(files=unknown, unresolved=True)

    root = Path(project_root).resolve()
    labels_by_path: dict[str, set[str]] = {}
    skipped: list[str] = []
    for handle in handles:
        try:
            inspection = catalog.inspect(handle)
        except Exception as exc:  # noqa: BLE001 — one broken Target must not hide the others
            logger.warning("Cannot inspect simulation Target %s: %s", handle.identity, exc)
            skipped.append(handle.identity)
            continue
        label = _label(inspection.eda_tool or handle.eda_tool)
        for item in inspection.inputs:
            bucket = labels_by_path.setdefault(canonical_project_path(root, item.path), set())
            if label:
                bucket.add(label)
    files = tuple(
        (path, tuple(sorted(labels_by_path.get(_normalize(root, path), ())))) for path in paths
    )
    return ScopeSimulators(files=files, skipped=tuple(skipped))


def render_simulation_context(sims: ScopeSimulators) -> str:
    """Render the ``## Simulation Context`` prompt section."""
    lines = ["## Simulation Context", ""]
    for path, labels in sims.files:
        if labels:
            lines.append(f"- {path}: {', '.join(labels)}")
        else:
            reason = _UNRESOLVED_REASON if sims.unresolved else _NO_TARGET_REASON
            lines.append(f"- {path}: unknown ({reason})")
    lines.append("")
    if sims.skipped:
        lines.append(
            "Some simulation Targets could not be inspected "
            f"({', '.join(sims.skipped)}), so the simulators listed above may be incomplete. "
            "Treat a rule marked for a simulator that is not listed as if that file's "
            "simulator were unknown."
        )
        lines.append("")
    lines.append(
        "This context is authoritative. Do not infer a simulator from Makefiles, scripts, "
        "or other build files, and do not quote Target settings that are not shown here. "
        "A rule or check marked `[<Simulator> only]` is reported only for a file whose "
        "simulators include that simulator. For a file with an unknown simulator, report "
        "such a rule at most with disposition `advisory` and name the assumed simulator "
        "in the finding summary. Rules without a mark apply on every simulator."
    )
    return "\n".join(lines) + "\n"
