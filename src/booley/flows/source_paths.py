"""Map FuseSoC's exported source names back to Project paths.

Booley reports a source by its Project path. FuseSoC names its export
``src/<core>/<declared name>`` inside the build tree, so a tool that reads the
export prints that name. This module maps one to the other for any Flow that
reads tool output.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fusesoc.vlnv import Vlnv

from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle


def core_files_roots(catalog: TargetCatalog, handle: TargetHandle) -> dict[str, Path]:
    """The file root of every core in the Target's closure, keyed by VLNV."""
    root = handle.project_root.resolve()
    return {
        str(Vlnv(str(fusesoc_registry.read_core(core)["name"]))): fusesoc_registry.core_files_root(
            core, root
        )
        for core in catalog.core_closure((handle,)) or ()
    }


def _normalized(name: str) -> str:
    """A tool-printed or exported name in one comparable POSIX spelling."""
    return os.path.normpath(name.replace("\\", "/")).replace("\\", "/")


@dataclass(frozen=True)
class ProjectSourcePaths:
    """Project path of each in-Project file one Target's FuseSoC export names."""

    by_name: Mapping[str, str] = field(default_factory=dict[str, str])

    @classmethod
    def for_target(
        cls, handle: TargetHandle, resolved: fusesoc_registry.ResolvedTarget
    ) -> ProjectSourcePaths:
        """Map each resolved file named ``src/<core>/<name>`` to its Project path.

        A file without a core, outside the Target's cores, or whose source lies
        outside the Project Root has no Project path. When a core cannot be
        read, no name has one: the findings stay as the linter printed them.
        """
        root = handle.project_root.resolve()
        try:
            roots = core_files_roots(TargetCatalog.build(handle.project_root), handle)
        except FuseSocError:
            return cls()  # an unreadable core leaves every name as reported
        by_name: dict[str, str] = {}
        for item in resolved.files:
            if item.core is None or item.core not in roots:
                continue
            prefix = f"src/{Vlnv(item.core).sanitized_name}/"
            name = item.name.replace("\\", "/")
            if not name.startswith(prefix):
                continue
            source = Path(os.path.normpath(roots[item.core] / name[len(prefix) :]))
            if source.is_relative_to(root):
                by_name[_normalized(item.name)] = source.relative_to(root).as_posix()
        return cls(by_name)

    def project_path(self, reported: str) -> str:
        """The Project path for a name a tool printed, else ``reported`` unchanged."""
        return self.by_name.get(_normalized(reported), reported)
