"""Typed access to the project-owned Doctor Target matrix."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError


@dataclass(frozen=True, slots=True)
class DoctorTargetMatrix:
    """Resolved Doctor selectors from authored ``.core`` files."""

    seed_targets: tuple[str, ...]
    selected_keys: frozenset[tuple[str, str]]

    def is_selected(self, name: str, vlnv: str) -> bool:
        """Whether an enumerated Target belongs to the Doctor matrix."""
        return (name, vlnv) in self.selected_keys


def doctor_targets(project_root: Path, flow_name: str) -> tuple[str, ...]:
    """Return authored Doctor Target selectors for one Flow, failing soft."""
    try:
        return tuple(
            handle.selector
            for handle in TargetCatalog.build(project_root).list()
            if flow_name in handle.doctor_flows
        )
    except FuseSocError:
        return ()


def build_doctor_target_matrix(project_root: Path) -> DoctorTargetMatrix:
    """Resolve the complete Doctor matrix once for matching checks."""
    try:
        handles = TargetCatalog.build(project_root).list()
    except FuseSocError:
        handles = ()

    selected_handles = tuple(handle for handle in handles if handle.doctor_flows)
    seed = tuple(dict.fromkeys(handle.selector for handle in selected_handles))
    selected = {(handle.name, handle.vlnv) for handle in selected_handles}

    return DoctorTargetMatrix(seed, frozenset(selected))
