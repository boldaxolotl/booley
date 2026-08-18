"""core_security.py — ``.core`` provenance + confinement validation (ADR 0022 dec 21).

ADR 0022 decision 21 permits a ``.core``'s imperative sections (``generators`` and
``hooks``) **iff** they are (a) out of the agent's write Scope and read-only-mounted
— so the agent can neither author nor edit them — and (b) Session-Runtime-confined;
and it **rejects every ``fpga_impl`` hook** outright, because implementation Targets
may use a privileged commercial-tool policy. It also rejects **expr-params**
(decision 8: an SV expression cannot be a
faithful ``-G`` vlogparam, and Booley ships no expression evaluator).

This module is the validator that enforces those three rules. Read this line and
believe it: **"validated" here means provenance + confinement, NOT a content-safety
proof.** We do not read what a generator script *does* — only *who controls it* (is
it out of the agent's mutable Scope?) and *where it runs* (inside the Session
Runtime). The Session Runtime is the security boundary; a generator
running inside it is no more dangerous than the simulator binary beside it. The real
agent-immutability guarantee is the Scope pre-commit hook — this validator only
*verifies* the provenance that hook mechanically enforces, closing the authoring
window (decision 21's TOCTOU note).

The validator builds on the cheap, subprocess-free ``.core`` reader in
:mod:`booley.fusesoc_registry` (``discover_cores`` / ``read_core`` /
``enumerate_targets``): it parses the YAML to *validate* (reject implementation hooks, reject
expr-params, enforce read-only provenance), never to silently strip.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.dev_support.criteria import (
    EDA_TOOL_CRITERION_FAMILIES as EDA_EDA_TOOL_CRITERION_FAMILIES,
)
from booley.fusesoc_registry import (
    FuseSocError,
    core_target_eda_tool,
    discover_cores,
    read_core,
    selectable_core_closure,
)
from booley.harness.git_utils import scope_matches_file

logger = logging.getLogger(__name__)

# CAPI2 per-target hook phases. A ``hooks:`` block maps any of these to a list of
# top-level ``scripts:`` names that run at that phase of the flow.
_HOOK_PHASES: tuple[str, ...] = ("pre_build", "post_build", "pre_run", "post_run")

# CAPI2's literal parameter datatypes. A ``parameters:`` entry declaring any *other*
# datatype (e.g. the migration artifact ``datatype: expr``) is an expr-param: it
# cannot become a faithful ``-G`` vlogparam, and Booley has no evaluator for it
# (decision 8). The legacy configs.toml ``{kind: expr}`` marker is rejected the same
# way, in case a hand-migration transcribed it verbatim into the ``.core``.
_CAPI2_PARAM_DATATYPES: frozenset[str] = frozenset({"bool", "file", "int", "real", "str"})

# File extensions that mark a referenced token as a script/program file (used to
# pull the script path out of a ``cmd``/``command`` so its provenance can be
# checked). Not exhaustive — a token that resolves to an existing file is treated
# as a script regardless of extension.
_SCRIPT_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".sh", ".tcl", ".pl", ".rb", ".js", ".bash"}
)


def fpga_impl_eda_tools() -> frozenset[str]:
    """EDA tools whose Targets are FPGA implementation Targets.

    Derived from the criteria EDA-tool map so it stays in lockstep with the
    criterion-family map: an EDA tool that gates ``fpga_impl_ok`` is, by
    definition, an FPGA implementation EDA tool (today just ``vivado``;
    Quartus when added).  Every hook on such a Target is rejected (decision
    21), regardless of whether its tool arrives through a trusted mount.
    """
    return frozenset(
        eda_tool
        for eda_tool, families in EDA_EDA_TOOL_CRITERION_FAMILIES.items()
        if "fpga_impl_ok" in families
    )


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreViolation:
    """One ``.core`` security-validation failure (provenance/confinement, not content)."""

    kind: str
    """Machine-readable category: ``fpga_hook`` | ``expr_param`` | ``in_scope_script``
    | ``unconfinable_script`` (scope is the wildcard, so
    nothing can be confined)."""

    core_file: Path
    """The ``.core`` that carries the offending section."""

    message: str
    """Human-facing explanation + the decision-21/-8 remediation guidance."""

    target: str | None = None
    """The Target the violation belongs to, when target-scoped (hooks); ``None``
    for core-level findings (expr-params, scripts)."""


# ---------------------------------------------------------------------------
# Per-check helpers
# ---------------------------------------------------------------------------


def _check_fpga_hooks(core_doc: Mapping[str, Any], core_file: Path) -> list[CoreViolation]:
    """Reject every hook declared on an FPGA implementation Target (decision 21).

    Session Runtimes execute inside the container, never through a host-command
    broker.  The narrower rule remains because implementation hooks add
    uncontrolled side effects around a privileged commercial-tool policy; it is
    intentionally unrelated to unsupported simulator names.
    """
    fpga_eda_tools = fpga_impl_eda_tools()
    targets = core_doc.get("targets")
    if not isinstance(targets, Mapping):
        return []
    violations: list[CoreViolation] = []
    for name, target in targets.items():
        if not isinstance(target, Mapping):
            continue
        eda_tool = core_target_eda_tool(core_doc, name)
        if eda_tool not in fpga_eda_tools:
            continue
        hooks = target.get("hooks")
        declared = _declared_hook_phases(hooks)
        if not declared:
            continue
        violations.append(
            CoreViolation(
                kind="fpga_hook",
                core_file=core_file,
                target=name,
                message=(
                    f"fpga Target (EDA tool '{eda_tool}') declares hooks "
                    f"({', '.join(declared)}); implementation hooks are rejected "
                    "at the design-description boundary. Move the work into a "
                    "resolution-time generator or out of the .core entirely."
                ),
            )
        )
    return violations


def _declared_hook_phases(hooks: Any) -> list[str]:
    """Return the hook phases a ``hooks:`` block actually populates."""
    if not isinstance(hooks, Mapping):
        return []
    return [
        phase
        for phase in _HOOK_PHASES
        if hooks.get(phase)  # truthy: a non-empty list of script names
    ]


def _check_expr_params(core_doc: Mapping[str, Any], core_file: Path) -> list[CoreViolation]:
    """Reject parameters that are not faithful literal vlogparams (decision 8)."""
    parameters = core_doc.get("parameters")
    if not isinstance(parameters, Mapping):
        return []
    violations: list[CoreViolation] = []
    for name, spec in parameters.items():
        datatype = _param_datatype(spec)
        if datatype is None or datatype in _CAPI2_PARAM_DATATYPES:
            continue
        violations.append(
            CoreViolation(
                kind="expr_param",
                core_file=core_file,
                message=(
                    f"parameter '{name}' declares datatype '{datatype}', which is "
                    "not a CAPI2 literal (bool/file/int/real/str). An HDL "
                    "expression cannot be a faithful -G vlogparam and Booley ships "
                    "no evaluator: resolve it to a literal, or convert it to a "
                    "vlogdefine (decision 8)."
                ),
            )
        )
    return violations


def _param_datatype(spec: Any) -> str | None:
    """Return a parameter's declared datatype, or its legacy ``kind`` marker.

    A CAPI2 parameter is a ``{datatype, paramtype, default}`` mapping. A
    hand-migration of the legacy configs.toml ``{kind: expr}`` form is also
    caught: its ``kind`` is surfaced as the datatype so the expr-param check
    rejects it.
    """
    if not isinstance(spec, Mapping):
        return None
    datatype = spec.get("datatype")
    if isinstance(datatype, str) and datatype:
        return datatype
    kind = spec.get("kind")
    return str(kind) if kind else None


def _check_script_provenance(
    core_doc: Mapping[str, Any],
    core_file: Path,
    project_root: Path,
    scope: list[str],
) -> list[CoreViolation]:
    """Require every ``scripts:``/``generators:`` file to be out of the write Scope.

    A script *inside* the Scope is plainly agent-authored, so its provenance
    cannot be trusted → rejected. A wildcard Scope (the agent may write
    anything) means *nothing* can be confined → every imperative script is
    rejected.

    Caveat since ADR 0046: an out-of-Scope script is no longer *immutable*.
    Ticket Scope became advisory, so the agent can edit such a script and the
    Harness will commit the edit as a reported deviation — where it previously
    would have been dropped before reaching the branch. What survives is the
    weaker guarantee this check was always able to make on its own: the script
    is not something the ticket set out to author, and any change to it is
    visible in the deviation report and the branch diff. Genuine immutability
    needs a read-only mount, which decision 21 named and Booley does not yet
    implement.
    """
    script_specs = _imperative_specs(core_doc)
    if not script_specs:
        return []  # the common case: no imperative sections, nothing to confine

    wildcard = scope == ["*"]
    violations: list[CoreViolation] = []
    for section, spec_name, spec in script_specs:
        for rel in _script_paths(spec, core_file, project_root):
            if wildcard:
                violations.append(
                    CoreViolation(
                        kind="unconfinable_script",
                        core_file=core_file,
                        message=(
                            f"{section} '{spec_name}' references '{rel}', but the "
                            "agent's write Scope is unrestricted ('*'), so the "
                            "script cannot be guaranteed read-only/immutable. "
                            "Imperative .core sections are only permitted with a "
                            "bounded Scope that excludes the script (decision 21)."
                        ),
                    )
                )
            elif scope_matches_file(scope, rel):
                violations.append(
                    CoreViolation(
                        kind="in_scope_script",
                        core_file=core_file,
                        message=(
                            f"{section} '{spec_name}' references '{rel}', which is "
                            "inside the agent's write Scope and therefore mutable. "
                            "generators/hooks scripts must be out-of-Scope and "
                            "read-only-mounted so the agent cannot author or edit "
                            "them (decision 21)."
                        ),
                    )
                )
    return violations


def _imperative_specs(
    core_doc: Mapping[str, Any],
) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Yield ``(section, name, spec)`` for every top-level script and generator."""
    specs: list[tuple[str, str, Mapping[str, Any]]] = []
    for section in ("scripts", "generators"):
        block = core_doc.get(section)
        if not isinstance(block, Mapping):
            continue
        for name, spec in block.items():
            if isinstance(spec, Mapping):
                specs.append((section.rstrip("s"), str(name), spec))
    return specs


def _script_paths(spec: Mapping[str, Any], core_file: Path, project_root: Path) -> list[str]:
    """Extract project-root-relative paths of the files a script/generator runs.

    CAPI2 ``scripts`` use ``cmd: [...]``; ``generators`` use ``command:`` (+ an
    optional ``interpreter``). We pull out file-like tokens — those that resolve
    to a real file under the core dir, or carry a script extension — and return
    them relative to *project_root* so they compare against Scope entries (which
    are project-root-relative). Tokens that escape the project (``..``) are kept
    as their resolved absolute string so they still surface in a violation.
    """
    rels: list[str] = []
    for token in _command_tokens(spec):
        if token.startswith("-") or ("/" not in token and not _looks_like_script(token)):
            continue
        candidate = (core_file.parent / token).resolve()
        if not (candidate.exists() or _looks_like_script(token)):
            continue
        try:
            rel = candidate.relative_to(project_root).as_posix()
        except ValueError:
            rel = str(candidate)  # outside the project tree
        if rel not in rels:
            rels.append(rel)
    return rels


def _command_tokens(spec: Mapping[str, Any]) -> list[str]:
    """Flatten a script/generator command spec into individual tokens."""
    tokens: list[str] = []
    for key in ("cmd", "command"):
        value = spec.get(key)
        if isinstance(value, str):
            tokens.extend(value.split())
        elif isinstance(value, (list, tuple)):
            tokens.extend(str(t) for t in value)
    return tokens


def _looks_like_script(token: str) -> bool:
    """True when a token's suffix marks it as a script/program file."""
    return Path(token).suffix in _SCRIPT_EXTENSIONS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_core(
    core_doc: Mapping[str, Any],
    *,
    core_file: Path | str | None = None,
    project_root: Path | str | None = None,
    scope: list[str] | None = None,
) -> list[CoreViolation]:
    """Validate one parsed ``.core`` for provenance + confinement (decision 21).

    Always runs the two scope-independent structural checks — reject
    ``fpga_impl`` hooks, reject expr-params. The script-provenance check
    (generators/hooks must be out-of-Scope) runs only when *project_root* and
    *scope* are both supplied; without a Scope there is nothing to check
    provenance *against*, so it is skipped (the caller is doing a structural-only
    pass). Returns the list of violations — empty means the ``.core`` passed.
    """
    cf = Path(core_file) if core_file is not None else Path("<unknown.core>")
    violations = _check_fpga_hooks(core_doc, cf)
    violations += _check_expr_params(core_doc, cf)
    if project_root is not None and scope is not None:
        violations += _check_script_provenance(core_doc, cf, Path(project_root), scope)
    return violations


def validate_project_cores(
    project_root: Path | str,
    *,
    scope: list[str] | None = None,
    seed_targets: Collection[str] | None = None,
) -> list[CoreViolation]:
    """Validate the authored ``.core`` files under *project_root* (decision 21).

    Discovers cores via :func:`fusesoc_registry.discover_cores` (generated build
    trees skipped) and aggregates the violations across them. Pass *scope* (the
    agent's write Scope) to additionally enforce script provenance; omit it for a
    structural-only audit.

    When *seed_targets* names the project's declared Targets (ADR 0030: the
    ``[flows.<flow>].default_target`` set), the audit is restricted to the cores reachable
    from those Targets' dependency closures
    (:func:`fusesoc_registry.selectable_core_closure`) — on a 208-core monorepo an
    unselectable core's in-Scope generator script must not FAIL doctor (SETUP-19).
    With no seed the closure is ``None`` and every discovered core is audited,
    exactly as before.
    """
    root = Path(project_root)
    audit_scope = selectable_core_closure(root, seed_targets)
    violations: list[CoreViolation] = []
    for core_file in discover_cores(root):
        if audit_scope is not None and core_file not in audit_scope:
            continue  # not reachable from any selectable Target — out of audit scope
        try:
            doc = read_core(core_file)
        except FuseSocError as exc:
            logger.debug("skipping unreadable .core %s: %s", core_file, exc)
            continue
        violations += validate_core(doc, core_file=core_file, project_root=root, scope=scope)
    return violations
