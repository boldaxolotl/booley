"""Goal completion composes runtime-resolved semantic Target input views."""

import copy
from pathlib import Path
from typing import Any

from booley.config.project_config import load_test_configuration, lookup_target_section
from booley.goals.model import GoalRecord
from booley.targets.catalog import TargetCatalog
from booley.targets.surface_diff import parameter_definitions


def resolved_goal_targets(root: Path, record: GoalRecord) -> dict[str, str]:
    """Resolve authored selectors through the same pinned catalog as semantic deltas."""
    catalog = TargetCatalog.build(root)
    return {
        goal.spec.target: catalog.select(goal.spec.target).identity
        for goal in record.goals
        if goal.spec.target is not None
    }


def resolved_surfaces(root: Path) -> dict[str, Any]:
    """Resolve selection, inputs, parameters and actual runtime test lookup."""
    catalog = TargetCatalog.build(root)
    tests = load_test_configuration(root)
    surfaces: dict[str, Any] = {}
    for handle in catalog.list():
        inspection = catalog.inspect(handle)
        declarations = parameter_definitions(
            handle.core_file.read_bytes(), path=handle.core_file.relative_to(root).as_posix()
        )
        selected = {
            name: row.body
            for name, row in declarations.items()
            if handle.identity in row.referenced_by and name in inspection.parameters
        }
        surfaces[handle.identity] = {
            "name": handle.name,
            "parameters": dict(inspection.parameters),
            "parameter_declarations": selected,
            "toplevel": inspection.toplevel,
            "filesets": [
                {
                    "path": item.path,
                    "type": item.file_type,
                    "attributes": dict(item.attributes),
                    "tags": list(item.tags),
                }
                for item in inspection.inputs
            ],
            "defines": {
                name: inspection.parameters[name]
                for name, declaration in selected.items()
                if declaration.get("paramtype") == "vlogdefine"
            },
            "tests": lookup_target_section(tests, handle.selector),
            "flow_options": dict(inspection.flow_options),
            "tool_options": dict(inspection.tool_options),
        }
    return surfaces


def project_target_changes(
    changes: list[dict[str, Any]], before_excluded: frozenset[str], after_excluded: frozenset[str]
) -> list[dict[str, Any]]:
    """Omit unproved artifact inventories while preserving authored semantic deltas."""
    result = copy.deepcopy(changes)
    for row in result:
        for side, excluded in (("before", before_excluded), ("after", after_excluded)):
            surface = row.get(side)
            if isinstance(surface, dict) and isinstance(surface.get("filesets"), list):
                surface["filesets"] = [
                    item for item in surface["filesets"] if item["path"] not in excluded
                ]
    return result
