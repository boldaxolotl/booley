"""Project readiness and bounded, explicit guidance/projection reconciliation.

These fixed operations run at separate composition points so Doctor can retain
its config, upgrade, guidance, Git, line-ending, projection, orphan ordering.
Inspection does not repair; reconciliation only uses the existing guidance and
projection owners. No command rendering, Sandbox issuance, or full Init here.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.audit import agent_schema, config_common, configs_schema, flow_schema, project_schema
from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.config.eda import EdaConfig, parse_eda_config
from booley.fusesoc import core_projection, fusesoc_registry
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.harness.setup.guidance_links import (
    CANON_NAME,
    ensure_guidance_links,
    guidance_links_current,
)
from booley.runtime.git import _git_common_dir
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.catalog import TargetCatalog

_REQUIRED_FLOW_TABLES = ("sim", "lint", "synth")
_GUIDANCE_BTOOL_MARKER = "booley_status"
_GUIDANCE_SANDBOX_MARKERS = ("inside the sandbox", "session runtime")
_STATE_TRANSIENT_DIR_NAMES = frozenset({"worktrees", "build", "_build", ".runtime", ".git", "tmp"})


class ReadinessMode(StrEnum):
    """Whether readiness may repair generated guidance links and projections."""

    INSPECT = "inspect"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class ProjectAudit:
    """Validated setup inputs; later findings do not invalidate this snapshot."""

    project_root: Path
    project_dir: Path
    booley_toml: dict[str, Any]
    configs_toml: dict[str, dict[str, Any]]
    first_target: str
    eda: dict[str, EdaConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectLoadResult:
    """Resolved data location survives even a failed configuration audit."""

    report: DiagnosticReport
    project_dir: Path | None
    project: ProjectAudit | None


def load_project(project_root: Path) -> ProjectLoadResult:
    """Read and validate checkout-local configuration, without repairs."""
    report = Findings()
    try:
        project_dir = resolve_checkout_project_dir(project_root)
    except FileNotFoundError:
        report.fail("project directory not found", "booley init")
        return ProjectLoadResult(report.report(), None, None)
    project = _check_project_setup(project_root, project_dir, report)
    return ProjectLoadResult(report.report(), project_dir, project)


def check_guidance(project: ProjectAudit, *, mode: ReadinessMode) -> DiagnosticReport:
    """Inspect canonical guidance; RECONCILE may repair generated root links."""
    report = Findings()
    _check_agents_md(project, report, repair=mode is ReadinessMode.RECONCILE)
    return report.report()


def check_stealth_cores(project: ProjectAudit, *, mode: ReadinessMode) -> DiagnosticReport:
    """Grade authored cores; RECONCILE may refresh generated projections."""
    report = Findings()
    stealth = project.booley_toml.get("stealth")
    enabled = isinstance(stealth, Mapping) and stealth.get("enabled") is True
    _check_stealth_cores(
        project.project_root,
        project.project_dir,
        report,
        stealth_enabled=enabled,
        repair=mode is ReadinessMode.RECONCILE,
    )
    return report.report()


def _check_project_setup(
    project_root: Path, project_dir: Path, report: Findings
) -> ProjectAudit | None:
    """Strictly parse and validate Booley project setup files."""
    report.pass_(f"project directory found: {project_dir}")

    booley_toml = _load_toml(project_dir / "booley.toml", report)
    if booley_toml is None:
        return None

    valid = _validate_booley_toml(booley_toml, project_dir, report)
    from booley.targets.flow_names import canonicalize_config

    booley_toml = canonicalize_config(booley_toml)

    # configs.toml is optional now: ADR 0022 makes the ``.core`` the sole
    # design-description home, and the legacy configs.toml registry path was
    # removed. Validate a configs.toml only when a project still ships one.
    configs_toml: dict[str, dict[str, Any]] = {}
    configs_path = project_dir / "configs.toml"
    if configs_path.is_file():
        configs_raw = _load_toml(configs_path, report)
        if configs_raw is None:
            return None
        validated = _validate_configs_toml(configs_raw, report)
        if validated is None:
            valid = False
        else:
            configs_toml = validated

    first_target = _first_target(project_root, configs_toml)

    if not valid:
        return None

    if first_target:
        report.pass_(f"first deep-check config: {first_target}")
    return ProjectAudit(
        project_root=project_root,
        project_dir=project_dir,
        booley_toml=booley_toml,
        configs_toml=configs_toml,
        first_target=first_target,
        eda=parse_eda_config(booley_toml.get("eda")),
    )


def _first_target(project_root: Path, configs_toml: dict[str, dict[str, Any]]) -> str:
    first_target = ""
    try:
        targets = TargetCatalog.build(project_root).list()
        first_target = targets[0].selector if targets else ""
    except Exception:  # noqa: BLE001 — registry may be unavailable; fall back to a configs.toml config name
        first_target = ""
    if not first_target:
        first_target = next(iter(configs_toml), "")

    return first_target


def _load_toml(path: Path, report: Findings) -> dict[str, Any] | None:
    if not path.is_file():
        report.fail(f"{path.name} missing", "booley init")
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        report.fail(f"{path.name} does not parse: {exc}", f"fix {path}")
        return None
    except OSError as exc:
        report.fail(f"{path.name} unreadable: {exc}", f"check permissions on {path}")
        return None
    report.pass_(f"{path.name} parses")
    return data


def _validate_booley_toml(data: dict[str, Any], project_dir: Path, report: Findings) -> bool:
    """Validate the project-level booley.toml schema used by doctor."""
    if not _add_config_audit(project_schema.audit_eda_config(data), report):
        return False
    valid = _add_config_audit(project_schema.audit_project_table(data), report)
    valid &= _add_config_audit(agent_schema.audit_agent_table(data), report)
    valid &= _add_config_audit(agent_schema.audit_models_table(data), report)
    valid &= _add_config_audit(project_schema.audit_feedback_table(data), report)
    valid &= _add_config_audit(project_schema.audit_stealth_table(data), report)
    valid &= _add_config_audit(flow_schema.audit_flow_tables(data, _REQUIRED_FLOW_TABLES), report)
    valid &= _add_config_audit(project_schema.audit_sandbox_table(data), report)
    valid &= _add_config_audit(project_schema.audit_interactive_table(data), report)
    valid &= _add_config_audit(project_schema.audit_developer_table(data), report)
    _add_config_audit(project_schema.audit_known_tables(data), report)
    # Source RTL/TB layout is validated from the .core tags:[tb] partition (see
    # _run_core_checks → sim_target_has_untagged_tb), not a booley.toml
    # [sources.*] table — those fields were retired (ADR 0026 follow-through).

    if not project_dir.exists():
        report.fail(f"project directory missing: {project_dir}", "booley init")
        valid = False
    return valid


def _validate_configs_toml(
    raw: dict[str, Any], report: Findings
) -> dict[str, dict[str, Any]] | None:
    audit = configs_schema.audit_configs_toml(raw)
    for issue in audit.issues:
        report.fail(issue.message, issue.fix)
    if audit.configs is None:
        return None
    report.pass_(f"configs.toml contains {len(audit.configs)} valid config(s)")
    return audit.configs


def _add_config_audit(audit: config_common.ConfigTableAudit, report: Findings) -> bool:
    for finding in audit.findings:
        if finding.severity is config_common.ConfigFindingSeverity.PASS:
            report.pass_(finding.message)
        elif finding.severity is config_common.ConfigFindingSeverity.FAIL:
            report.fail(finding.message, finding.fix)
        else:
            assert finding.check_id is not None
            report.warn(
                finding.message, finding.fix, check_id=finding.check_id, subject=finding.subject
            )
    return audit.is_valid


def _check_agents_md(project: ProjectAudit, report: Findings, *, repair: bool) -> None:
    """Check the canonical guidance file and ensure root links point to it.

    The canonical AGENTS.md lives in the project data dir; the RTL repo root
    only carries generated AGENTS.md/CLAUDE.md links to it.
    """
    canon = project.project_dir / CANON_NAME
    if not canon.is_file():
        report.note("project guidance file missing; run setup guidance when ready")
        return
    if repair:
        try:
            ensure_guidance_links(project.project_root, project.project_dir)
            report.pass_("project guidance file present; root links ensured")
        except OSError as exc:
            _guidance_warning(report, f"could not create root guidance links: {exc}")
    elif guidance_links_current(project.project_root, canon):
        report.pass_("project guidance file present; root links current")
    else:
        _guidance_warning(
            report,
            "project guidance root links are missing or stale",
            "run `booley doctor` to repair AGENTS.md and CLAUDE.md",
        )
    _check_guidance_runtime_note(canon, report)


def _guidance_warning(report: Findings, message: str, fix: str = "") -> None:
    report.warn(message, fix, check_id="guidance.links-unhealthy")


def _check_guidance_runtime_note(canon: Path, report: Findings) -> None:
    """The guidance must scope its Booley Flow instructions to the Sandbox."""
    try:
        text = canon.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as exc:
        _scope_warning(canon, report, f"could not read {canon}: {exc}")
        return
    if _GUIDANCE_BTOOL_MARKER not in text:
        return  # no Booley Flow instructions to scope
    if any(marker in text for marker in _GUIDANCE_SANDBOX_MARKERS):
        report.pass_("project guidance scopes Booley Flows to the Sandbox")
        return
    _scope_warning(
        canon,
        report,
        "project guidance tells agents to call booley_status and the Booley Flows but never says "
        "they exist only inside the Sandbox — a host-side agent session sees no such "
        "Booley Flows and falls back to raw EDA commands",
        f"add the Sandbox scoping bullet from AGENTS_TEMPLATE.md to {canon}",
    )


def _scope_warning(canon: Path, report: Findings, message: str, fix: str = "") -> None:
    report.warn(message, fix, check_id="guidance.session-runtime-scope", subject=str(canon))


def _check_stealth_cores(
    project_root: Path, project_dir: Path, report: Findings, *, stealth_enabled: bool, repair: bool
) -> None:
    """Diagnose stranded/colliding authored cores and inspect or repair projections."""
    stealth_root = project_dir / fusesoc_registry.STATE_CORES_SUBDIR
    authored = tuple(sorted(stealth_root.rglob("*.core"))) if stealth_root.is_dir() else ()
    if stealth_enabled is False and authored:
        report.fail(
            ".booley_project/cores contains authored cores while stealth mode is disabled",
            "set [stealth] enabled = true, or move the cores into the tracked repository",
        )
    elif stealth_enabled is True:
        _check_core_projections(project_root, report, repair=repair)

    stranded = [
        p
        for p in project_dir.rglob("*.core")
        if not p.is_relative_to(stealth_root)
        and not _STATE_TRANSIENT_DIR_NAMES.intersection(p.relative_to(project_dir).parts)
        and not any(part.startswith(".baseline-wt") for part in p.relative_to(project_dir).parts)
        and TRACE_OVERLAY_MARKER not in p.name
    ]
    if stranded:
        names = ", ".join(str(p.relative_to(project_dir)) for p in sorted(stranded))
        report.fail(
            f"authored .core stranded in the state dir, invisible to Booley and FuseSoC: {names}",
            f"move it under {stealth_root} — the one scanned subtree of .booley_project/ (ADR 0036)",
        )
    else:
        report.pass_("no authored .core stranded outside .booley_project/cores/")

    try:
        TargetCatalog.build(project_root).list()
    except fusesoc_registry.CoreCollisionError as exc:
        report.fail(str(exc), "rename or delete one of the colliding cores")
    except fusesoc_registry.FuseSocError:
        # Unreadable/misnamed cores are the core-audit checks' beat, not ours.
        return
    else:
        report.pass_("no VLNV collision between repo-tree and .booley_project/cores/ cores")


def _check_core_projections(project_root: Path, report: Findings, *, repair: bool) -> None:
    """Audit or reconcile the derived root-level core copies."""
    failed = False
    if repair:
        try:
            core_projection.reconcile_projected_cores(project_root)
        except (core_projection.CoreProjectionError, OSError) as exc:
            report.fail(
                f"stealth core projection failed: {exc}", "fix the conflict and run booley init"
            )
            failed = True
    try:
        issues = core_projection.projection_issues(project_root)
    except (core_projection.CoreProjectionError, OSError) as exc:
        report.fail(
            f"could not inspect stealth core projections: {exc}",
            "fix the core and run booley init",
        )
        failed = True
        issues = ()
    if issues:
        report.fail(
            f"stealth core projections are not current: {', '.join(issues)}",
            "run booley init to reconcile the ignored root-level projections",
        )
    elif not failed:
        report.pass_("stealth core projections match .booley_project/cores/")
    _check_projection_exclude(project_root, report)


def _check_projection_exclude(project_root: Path, report: Findings) -> None:
    """Require generated core projections to stay out of host git status."""
    common = _git_common_dir(project_root)
    if common is None:
        return
    exclude = common / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    pattern = f"/{core_projection.PROJECTED_CORE_GLOB}"
    if pattern not in lines:
        report.fail(
            "stealth core projections are not excluded from host git status",
            "run booley init to add the generated projection pattern to .git/info/exclude",
        )
    else:
        report.pass_("stealth core projections are excluded through .git/info/exclude")
