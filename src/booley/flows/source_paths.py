"""Map FuseSoC's exported source names back to Project paths.

Booley reports a source by its Project path. FuseSoC names its export
``src/<core>/<declared name>`` inside the build tree, so a tool that reads the
export prints that name. This module maps one to the other for any Flow that
reads tool output.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
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
        own = str(Vlnv(handle.vlnv))
        if own in roots:
            # An overlay core is the Target's core under a derived VLNV, written
            # beside it, so its files share the Target's file root.
            roots.setdefault(str(Vlnv(resolved.vlnv)), roots[own])
        by_name: dict[str, str] = {}
        for item in resolved.files:
            if item.core is None or item.core not in roots:
                continue
            prefix = f"src/{Vlnv(item.core).sanitized_name}/"
            name = item.name.replace("\\", "/")
            if not name.startswith(prefix):
                continue
            source = Path(os.path.normpath(roots[item.core] / name[len(prefix) :]))
            if not source.is_file():
                continue  # a name FuseSoC staged (copyto) or generated has no source
            project_path = fusesoc_registry.canonical_project_path(root, source)
            if not Path(project_path).is_absolute():
                by_name[_normalized(item.name)] = project_path
        return cls(by_name)

    def with_spellings(self, spellings: Callable[[str], Iterable[str]]) -> ProjectSourcePaths:
        """This mapping, also keyed by each other spelling *spellings* gives an exported name.

        For a tool that runs outside the export root, so prints an exported file
        by another relative path or by its absolute path.
        """
        by_name = dict(self.by_name)
        for name, project_path in self.by_name.items():
            by_name.update((_normalized(spelling), project_path) for spelling in spellings(name))
        return ProjectSourcePaths(by_name)

    def project_path(self, reported: str) -> str:
        """The Project path for a name a tool printed, else ``reported`` unchanged."""
        return self.by_name.get(_normalized(reported), reported)

    @cached_property
    def _name_pattern(self) -> re.Pattern[str] | None:
        names = sorted(self.by_name, key=len, reverse=True)
        if not names:
            return None
        alternation = "|".join(re.escape(name) for name in names)
        return re.compile(rf"(?<![\w./-])(?:{alternation})(?![\w/-]|\.\w)")

    def project_text(self, text: str) -> str:
        """``text`` with each mapped build-tree name replaced by its Project path."""
        pattern = self._name_pattern
        if pattern is None:
            return text
        return pattern.sub(lambda match: self.by_name[match.group(0)], text)
