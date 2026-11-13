"""Diagnostic owner for the ``.core`` tree audit: setup hazards, CAPI2 schema, security.

Takes typed requests and returns a :class:`DiagnosticReport`; the caller (Doctor)
only renders it. The owner knows nothing of Doctor, CLI orchestration or
rendering. It decides which ``.core`` files the project owns, so a defect in an
owned core FAILs and the same defect in a vendored upstream core is a NOTE.

Permitted effects: reads ``.core`` files and lists the Project tree for
directory links; no subprocess, writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.fusesoc import core_security, fusesoc_registry
from booley.fusesoc.core_projection import lexical_stealth_path
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle

__all__ = [
    "CoreSecurityInspectionRequest",
    "CoreTreeInspectionRequest",
    "fusesoc_rejected_cores",
    "inspect_core_security",
    "inspect_core_tree",
    "owned_core_files",
    "rejected_target_core",
]


@dataclass(frozen=True, slots=True)
class CoreTreeInspectionRequest:
    """Inputs of the setup-hazard and schema audit of one ``.core`` tree."""

    project_root: Path  # the Project Root whose .core tree is audited
    seed_targets: tuple[str, ...]  # Doctor-selected seed Target selectors (ownership)


@dataclass(frozen=True, slots=True)
class CoreSecurityInspectionRequest:
    """Inputs of the provenance and confinement audit of one ``.core`` tree."""

    project_root: Path  # the Project Root whose .core tree is audited
    write_scope: tuple[str, ...]  # writable Scope globs the caller derives


def inspect_core_tree(request: CoreTreeInspectionRequest) -> DiagnosticReport:
    """Catch provider and recursive-symlink hazards, then CAPI2 schema violations."""
    findings = Findings()
    owned_cores = owned_core_files(request.project_root, request.seed_targets)
    _check_setup_hazards(request, owned_cores, findings)
    _check_schema(request, owned_cores, findings)
    return findings.report()


def inspect_core_security(request: CoreSecurityInspectionRequest) -> DiagnosticReport:
    """Report authored-core provenance and confinement violations (decision 21)."""
    findings = Findings()
    catalog = TargetCatalog.build(request.project_root)
    # The first handle per name, exactly as Doctor's Target enumeration keeps it;
    # both read the same cached TargetCatalog snapshot.
    refs: dict[str, TargetHandle] = {}
    for handle in catalog.list():
        refs.setdefault(handle.name, handle)
    violations = core_security.validate_project_cores(
        request.project_root,
        scope=list(request.write_scope),
        audit_scope=catalog.core_closure(tuple(refs.values())),
    )
    for violation in violations:
        target = f" target '{violation.target}'" if violation.target else ""
        line = (
            f".core security [{violation.kind}] {violation.core_file.name}"
            f"{target}: {violation.message}"
        )
        if violation.kind in _SCOPE_DEPENDENT_VIOLATIONS:
            findings.warn(
                f"{line} (advisory: doctor audits the union of writable "
                "category dirs, without a change-specific scope — the binding "
                "check runs at commit time)",
                check_id="core.security-scope-advisory",
                subject=f"{violation.core_file.name}:{violation.target or '-'}",
            )
        else:
            findings.fail(line, "ADR 0022 decision 21")
    if not violations and refs:
        findings.pass_(
            ".core security validation passed (no fpga hooks / expr-params / in-scope scripts)"
        )
    return findings.report()


#: ``.core`` security violations whose verdict depends on the agent's write
#: Scope. doctor only has a synthetic project-wide Scope, so these are
#: advisory there; the execution Scope check at commit time is the real gate.
#: Everything else (``fpga_hook``, ``expr_param``) is a property of the
#: ``.core`` itself and stays a hard FAIL.
_SCOPE_DEPENDENT_VIOLATIONS = frozenset({"in_scope_script", "unconfinable_script"})


def owned_core_files(project_root: Path, seed_targets: Iterable[str]) -> set[Path]:
    """The ``.core`` files whose findings the project can act on (ADR 0036).

    A core is "owned" when it hosts a Target selected by Doctor metadata (the
    project drives it, so its defects are the project's problem) or lives in the
    state zone (``.booley/cores/`` is always Booley-authored). Everything
    else is vendored upstream content — advice like "fix the .core" is
    unfollowable for a repo that must stay byte-identical to upstream.
    """
    owned: set[Path] = set()
    try:
        catalog = TargetCatalog.build(project_root)
    except FuseSocError:
        catalog = None
    if catalog is not None:
        for token in seed_targets:
            try:
                owned.add(lexical_stealth_path(project_root, catalog.select(token).core_file))
            except FuseSocError:
                continue  # unresolvable config tokens are their own doctor finding
    for core_file in fusesoc_registry.discover_cores(project_root):
        if fusesoc_registry.is_stealth_core(project_root, core_file):
            owned.add(core_file)
    return owned


def fusesoc_rejected_cores(
    project_root: Path,
) -> dict[Path, fusesoc_registry.FuseSocCoreRejection]:
    """Map every discovered ``.core`` FuseSoC ignores to its rejection."""
    rejected: dict[Path, fusesoc_registry.FuseSocCoreRejection] = {}
    for core_file in fusesoc_registry.discover_cores(project_root):
        rejection = _fusesoc_core_rejection(core_file)
        if rejection is not None:
            rejected[core_file] = rejection
    return rejected


def rejected_target_core(
    project_root: Path,
    target: str,
    rejected_cores: Mapping[Path, fusesoc_registry.FuseSocCoreRejection],
) -> Path | None:
    """Return the FuseSoC-rejected core hosting Doctor Target *target*, if any."""
    if not rejected_cores:
        return None
    try:
        core_file = TargetCatalog.build(project_root).select(target).core_file
    except FuseSocError:
        return None  # target-resolution diagnostics report this elsewhere
    core_file = lexical_stealth_path(project_root, core_file)
    return core_file if core_file in rejected_cores else None


# ---------------------------------------------------------------------------
# Setup hazards
# ---------------------------------------------------------------------------

_RECURSIVE_SYMLINK_FIX = (
    "add a FUSESOC_IGNORE marker in each link's containing directory or an enclosing "
    "subtree (excludes all cores there); ensure markers are present in worktrees too, "
    "or remove the links"
)


def _check_setup_hazards(
    request: CoreTreeInspectionRequest, owned_cores: set[Path], findings: Findings
) -> None:
    """Catch offline-provider and recursive-symlink failures before FuseSoC."""
    root = request.project_root
    catalog = TargetCatalog.build(root)
    selected_handles = []
    for selector in request.seed_targets:
        try:
            selected_handles.append(catalog.select(selector))
        except FuseSocError:
            continue
    selected_closure = catalog.core_closure(selected_handles)
    required_cores = owned_cores | set(selected_closure or ())
    hazards = fusesoc_registry.core_setup_hazards(root)
    if not hazards:
        findings.pass_(".core tree has no provider or recursive-symlink setup hazards")
        return
    for hazard in hazards:
        if hazard.kind == "recursive-symlink":
            continue
        rel = hazard.path.relative_to(root)
        if hazard.path in required_cores:
            findings.fail(
                f"in-tree .core {rel} has a provider block that requests a remote fetch",
                "remove provider: from the in-tree core",
            )
        else:
            findings.note(
                f"vendored .core {rel} has a provider block, but no Doctor Target selects it"
            )
    _report_recursive_symlink_hazards(root, hazards, findings)


def _report_recursive_symlink_hazards(
    root: Path, hazards: list[fusesoc_registry.CoreSetupHazard], findings: Findings
) -> None:
    """Group resolved ancestors without conflating unresolved chain cycles."""
    groups: dict[Path, list[Path]] = {}
    for hazard in hazards:
        if hazard.kind != "recursive-symlink":
            continue
        if hazard.target is None:
            rel = hazard.path.relative_to(root).as_posix()
            findings.fail(
                f"FuseSoC recursive symlink {rel}: {hazard.detail}", _RECURSIVE_SYMLINK_FIX
            )
        else:
            groups.setdefault(hazard.target, []).append(hazard.path)
    for target in sorted(groups, key=str):
        _report_recursive_symlink_group(root, target, groups[target], findings)


def _report_recursive_symlink_group(
    root: Path, target: Path, paths: list[Path], findings: Findings
) -> None:
    """Summarize one ancestor and expose every link as a verbose-only detail."""
    relative = sorted(path.relative_to(root).as_posix() for path in paths)
    shown = ", ".join(relative[:3])
    if len(relative) > 3:
        shown += f", +{len(relative) - 3} more; run booley doctor --verbose for all link paths"
    noun = "link" if len(relative) == 1 else "links"
    verb = "resolves" if len(relative) == 1 else "resolve"
    findings.fail(
        f"FuseSoC recursive symlinks: {len(relative)} {noun} {verb} "
        f"to ancestor {target} ({shown})",
        _RECURSIVE_SYMLINK_FIX,
    )
    for path in relative:
        findings.detail(
            f"  recursive symlink {path}: directory link resolves to ancestor {target}"
        )


# ---------------------------------------------------------------------------
# CAPI2 schema
# ---------------------------------------------------------------------------

_CORE_SCHEMA_FIX = (
    "fix the .core — FuseSoC's CAPI2 schema requires this "
    "(caught here so it does not only surface under --deep)"
)


def _check_schema(
    request: CoreTreeInspectionRequest, owned_cores: set[Path], findings: Findings
) -> None:
    """Flag CAPI2 schema violations in any discovered ``.core``.

    Booley's tolerant ``yaml.safe_load`` accepts a scalar — or a bare ``depend:``
    that parses to ``None`` — where FuseSoC's schema demands an array, and an
    unknown property (a ``booley:`` mapping beside ``flow_options``); cheap
    enumeration then greenlights a ``.core`` the deep resolve later rejects.
    Reproduce FuseSoC's verdict host-side (no subprocess): Booley's array-field
    subset plus FuseSoC's own parser.

    Severity follows ownership. A violation FAILs when the core is one the
    project actually drives — it hosts a Doctor-selected Target — or
    lives in the state zone (``.booley/cores/``, always Booley-authored,
    ADR 0036). A vendored upstream core the project never selects (the pristine
    YosysHQ ``picorv32.core`` ships a bare ``depend:``) is a NOTE instead:
    FuseSoC drops such a core at resolve time, so only *its own* targets are
    unusable — the configured flows are unaffected, and "fix the .core" is
    unfollowable advice for a repo that must stay byte-identical to upstream.
    """
    schema_clean = True
    for core_file in fusesoc_registry.discover_cores(request.project_root):
        core_findings, rejection = _core_schema_findings(core_file)
        if not core_findings:
            continue
        schema_clean = False
        # A misplaced ``booley:`` marker is the project's own Doctor intent that
        # FuseSoC's rejection hides from Booley's reader, so the core is driven.
        owned = core_file in owned_cores or (rejection is not None and rejection.misplaced_booley)
        for msg, fix in core_findings:
            if owned:
                findings.fail(f".core schema {core_file.name}: {msg}", fix)
            else:
                findings.note(
                    f".core schema {core_file.name}: {msg} — vendored core, no "
                    "Doctor Target selects it; FuseSoC skips it at resolve, "
                    "so only that core's own Targets are unusable"
                )
    if schema_clean:
        findings.pass_(".core CAPI2 schema valid")


def _core_schema_findings(
    core_file: Path,
) -> tuple[list[tuple[str, str]], fusesoc_registry.FuseSocCoreRejection | None]:
    """Return ``([(message, fix), ...], FuseSoC's rejection)`` for one ``.core``.

    Booley's host-side subset runs first; FuseSoC's own verdict follows as one
    more row unless a FuseSoC-class subset row (array fields, per-file keys)
    already describes the same CAPI2 entry. Booley-only metadata rows never
    suppress it: they do not say why FuseSoC ignores the file.
    """
    findings = [(msg, _CORE_SCHEMA_FIX) for msg in fusesoc_registry.core_schema_errors(core_file)]
    rejection = _fusesoc_core_rejection(core_file)
    if rejection is None:
        return findings, None
    entry = rejection.entry
    if entry is not None and any(
        msg == entry or msg.startswith((f"{entry}.", f"{entry} "))
        for msg, _ in findings
        if ".flow_options.booley" not in msg
    ):
        return findings, rejection
    fix = _CORE_SCHEMA_FIX
    if rejection.misplaced_booley:
        fix = f"move the booley: mapping under targets.{rejection.target}.flow_options"
    findings.append(
        (
            f"{rejection.message} — FuseSoC ignores this whole .core, "
            "so none of its Targets resolve",
            fix,
        )
    )
    return findings, rejection


def _fusesoc_core_rejection(
    core_file: Path,
) -> fusesoc_registry.FuseSocCoreRejection | None:
    """FuseSoC's verdict on a readable ``.core``; unreadable files have their own row."""
    try:
        fusesoc_registry.read_core(core_file)
    except FuseSocError:
        return None
    return fusesoc_registry.fusesoc_core_rejection(core_file)
