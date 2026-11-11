"""Cocotb Target diagnostics: the rules Doctor renders, owned next to the backend.

ADR 0034 / F1. A Target that declares ``flow_options.cocotb_module`` is checked
for a tests.toml ``select`` template contradiction, for its module file in a
``tb``-tagged fileset, and for tests.toml test names against the module's
source. The module source is parsed with :mod:`ast` and never imported or
executed.

The owner takes a typed request and returns a :class:`DiagnosticReport`; the
caller decides how to render it. It reads ``.core`` files and the cocotb
module source and writes nothing.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.config.project_config import lookup_target_section
from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle
from booley.targets.selection import TargetSectionConflictError


@dataclass(frozen=True)
class CocotbTargetRequest:
    """Selectable Targets and normalized tests.toml sections for the cocotb rules."""

    #: Each selectable Target once; the owner picks the cocotb ones.
    targets: tuple[TargetHandle, ...]
    #: ``normalize_tests_toml`` output; empty when tests.toml is absent or invalid.
    tests_sections: Mapping[str, Any]


def project_has_cocotb_target(project_root: Path) -> bool:
    """True when any selectable Target declares a ``cocotb_module`` (ADR 0034).

    Cheap ``.core`` YAML read; gates the conditional cocotb checks so a
    plain-SV project is never faulted for a capability it doesn't use.
    """
    try:
        handles = TargetCatalog.build(project_root).list()
    except FuseSocError:
        return False
    return any(handle.cocotb_module for handle in handles)


def inspect_cocotb_targets(request: CocotbTargetRequest) -> DiagnosticReport:
    """Conditional Cocotb Target checks (ADR 0034 / F1).

    Fires only for Targets that declare ``flow_options.cocotb_module``:

    * a tests.toml plusarg ``select`` template on it is a config
      contradiction (selection is the ``COCOTB_TEST_FILTER`` env var) — FAIL;
    * the module file should exist in the Target's filesets and its fileset
      be tagged ``tb`` (Source Isolation) — advisory WARN;
    * plain test functions support advisory static name checks. Generating
      modules defer every registered name, including generating base names,
      to results.xml at run time (decision 7).
    """
    findings = Findings()
    cocotb_targets = sorted(
        (target for target in request.targets if target.cocotb_module),
        key=lambda target: target.name,
    )
    for target in cocotb_targets:
        _inspect_target(target, request.tests_sections, findings)
    return findings.report()


def _inspect_target(
    target: TargetHandle,
    sections: Mapping[str, Any],
    findings: Findings,
) -> None:
    """Record one cocotb Target's findings."""
    name = target.name
    module = target.cocotb_module or ""
    try:
        section = lookup_target_section(sections, f"{target.vlnv}#{target.name}") or {}
    except TargetSectionConflictError:
        return  # the section-key audit reports the duplicate keys
    if "select" in section:
        findings.fail(
            f"cocotb Target '{name}' declares a tests.toml `select` plusarg template",
            "remove `select` — cocotb test selection is the "
            "COCOTB_TEST_FILTER env var built from the `tests` list "
            "(ADR 0034)",
        )
    found = _module_file(target, module)
    if found is None:
        findings.warn(
            f"cocotb Target '{name}': no fileset file matches its "
            f"cocotb_module '{module}' ({module}.py)",
            f"add the Python testbench to a tagged fileset with copyto: {module}.py",
            check_id="cocotb.module-file-missing",
            subject=name,
        )
        return
    source, tagged = found
    if not tagged:
        findings.warn(
            f"cocotb Target '{name}': the fileset carrying {module}.py is not tagged tags:[tb]",
            "tag it so Source Isolation partitions RTL vs TB (ADR 0022 dec 13 / ADR 0034 dec 4)",
            check_id="cocotb.tb-tag-missing",
            subject=name,
        )
    if not source.is_file():
        findings.warn(
            f"cocotb Target '{name}': module file {source} not found on disk",
            "author the Python testbench (or fix the fileset path)",
            check_id="cocotb.module-file-missing",
            subject=name,
        )
        return
    findings.pass_(f"cocotb Target '{name}' module file present ({module}.py)")
    declared = section.get("tests") or []
    if declared:
        _check_test_names(source, module, name, declared, findings)


def _module_file(target: TargetHandle, module: str) -> tuple[Path, bool] | None:
    """Locate the Target's cocotb module file among its filesets.

    Matches a fileset entry whose ``copyto`` destination or source basename is
    ``<module>.py``. Returns ``(source path, fileset tb-tagged)`` or ``None``.
    """
    try:
        doc = fusesoc_registry.read_core(target.core_file)
    except FuseSocError:
        return None
    targets = doc.get("targets") or {}
    target_def = targets.get(target.name) if isinstance(targets, dict) else None
    if not isinstance(target_def, dict):
        return None
    filesets = doc.get("filesets") or {}
    want = f"{module}.py"
    for fs_name in fusesoc_registry.target_fileset_names(target_def):
        fileset = filesets.get(fs_name)
        if not isinstance(fileset, dict):
            continue
        tags = fileset.get("tags") or []
        tagged = isinstance(tags, list) and "tb" in tags
        for entry in fileset.get("files") or []:
            if isinstance(entry, str):
                path, attrs = entry, {}
            elif isinstance(entry, dict) and len(entry) == 1:
                path, attrs = next(iter(entry.items()))
                attrs = attrs if isinstance(attrs, dict) else {}
            else:
                continue
            copyto = str(attrs.get("copyto", ""))
            if Path(str(path)).name == want or copyto == want or copyto.endswith(f"/{want}"):
                return target.core_file.parent / str(path), tagged
    return None


# A cocotb module can generate rendered test IDs at import time through legacy
# ``TestFactory`` or cocotb 2's ``@cocotb.parametrize`` decorator. Those IDs
# are registered at import time. Static inspection must never execute a module.


def _symbol(node: ast.expr, aliases: dict[str, str]) -> str:
    """Resolve source-visible import aliases for a decorator or factory call."""
    if isinstance(node, ast.Call):
        return _symbol(node.func, aliases)
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_symbol(node.value, aliases)}.{node.attr}"
    return ""


def _imports(tree: ast.Module) -> dict[str, str]:
    """Map each top-level import's local name to its dotted origin."""
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _source_names(tree: ast.Module) -> tuple[set[str], set[str], bool]:
    """Identify plain test functions and known generating bases, conservatively."""
    aliases = _imports(tree)
    plain: set[str] = set()
    bases: set[str] = set()
    generated = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        symbol = _symbol(node.func, aliases)
        if symbol.rsplit(".", 1)[-1] in {"TestFactory", "generate_tests", "parametrize"}:
            generated = True
        if symbol.rsplit(".", 1)[-1] == "TestFactory":
            arguments = node.args[:1] + [
                kw.value for kw in node.keywords if kw.arg == "test_function"
            ]
            bases.update(arg.id for arg in arguments if isinstance(arg, ast.Name))
        if symbol == "cocotb.test" and any(kw.arg == "name" for kw in node.keywords):
            generated = True
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        decorators = {_symbol(dec, aliases) for dec in node.decorator_list}
        if "cocotb.parametrize" in decorators:
            bases.add(node.name)
        elif decorators == {"cocotb.test"}:
            plain.add(node.name)
    return plain - bases, bases, generated


def _check_test_names(
    source: Path,
    module: str,
    target: str,
    declared: list[str],
    findings: Findings,
) -> None:
    """Report only the name claims static source can support."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
        plain, bases, generated = _source_names(tree)
    except (OSError, SyntaxError, ValueError, RecursionError):
        findings.skip(
            f"cocotb Target '{target}': tests.toml names cannot be verified statically — "
            f"{module}.py could not be read or parsed; check results.xml at run time"
        )
        return
    if generated:
        base_names = [name for name in declared if name in bases]
        explanation = (
            f"; generating function name(s): {', '.join(base_names)} — cocotb reports "
            "rendered IDs, which may differ from these base names"
            if base_names
            else ""
        )
        findings.skip(
            f"cocotb Target '{target}': {len(declared)} tests.toml name(s) "
            f"cannot be verified statically: {', '.join(declared)} — {module}.py uses "
            "factory/parameterized test generation or renamed test IDs; "
            f"the names are checked against results.xml at run time{explanation}"
        )
        return
    missing = [name for name in declared if name not in plain]
    if missing:
        findings.warn(
            f"cocotb Target '{target}': tests.toml names not found as "
            f"functions decorated with @cocotb.test in {module}.py: {', '.join(missing)} "
            "(static, advisory)",
            "check the names against the @cocotb.test functions — a "
            "mismatch surfaces at run time as inconclusive",
            check_id="cocotb.test-name-missing",
            subject=target,
        )
    else:
        findings.pass_(f"cocotb Target '{target}' tests.toml names all present in {module}.py")
