"""Typed access to the project-owned Doctor Target matrix."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.fusesoc import fusesoc_registry
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.targets import target_naming


@dataclass(frozen=True, slots=True)
class DoctorTargetMatrix:
    """Resolved Doctor selectors and naming axes from authored ``.core`` files."""

    seed_targets: tuple[str, ...]
    selected_keys: frozenset[tuple[str, str]]
    axes_by_target: tuple[tuple[str, str], ...]

    def is_selected(self, name: str, vlnv: str) -> bool:
        """Whether an enumerated Target belongs to the Doctor matrix."""
        return (name, vlnv) in self.selected_keys

    def axes(self) -> dict[str, str]:
        """Return bare Target names mapped to their naming axis."""
        return dict(self.axes_by_target)


def doctor_targets(project_root: Path, flow_name: str) -> tuple[str, ...]:
    """Return authored Doctor Target selectors for one Flow, failing soft."""
    try:
        return tuple(fusesoc_registry.doctor_target_selectors(project_root, flow_name))
    except FuseSocError:
        return ()


def build_doctor_target_matrix(project_root: Path) -> DoctorTargetMatrix:
    """Resolve the complete Doctor matrix once for matching and naming checks."""
    try:
        seed = tuple(fusesoc_registry.doctor_target_seed(project_root))
    except FuseSocError:
        seed = ()

    selected: set[tuple[str, str]] = set()
    for token in seed:
        try:
            ref = fusesoc_registry.resolve_ref(project_root, token)
        except FuseSocError:
            continue
        selected.add((ref.name, ref.vlnv))

    axes: dict[str, str] = {}
    try:
        declarations = fusesoc_registry.target_declarations(project_root)
    except FuseSocError:
        declarations = {}
    for name, refs in declarations.items():
        for ref in refs:
            for flow_name in ref.doctor_flows:
                axis = target_naming.AXIS_FOR_FLOW.get(flow_name)
                if axis is not None:
                    axes.setdefault(name, axis)

    return DoctorTargetMatrix(seed, frozenset(selected), tuple(axes.items()))
