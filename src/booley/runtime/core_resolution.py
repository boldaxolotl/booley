"""Diagnostic owner for resolving Doctor-selected ``.core`` Targets in the Sandbox.

Takes a typed request and returns a :class:`CoreResolutionResult`; the caller
(Doctor) only renders the report and records which selectors produced a verdict.
The owner knows nothing of Doctor, CLI orchestration or rendering. It decides
what a resolvable Target is: each selector is selected through the Project's
``TargetCatalog`` (a selection failure is that Target's FAIL), one resolver
runs inside the Sandbox for every selectable Target, and exactly one bounded
verdict per selected Target is required back (missing, incomplete, duplicate or
malformed evidence fails). A failing Target carries an always-shown excerpt of
its error. An unavailable Sandbox runtime skips.

Permitted effects: reads ``.core`` files through ``TargetCatalog``; runs one
``python3 -c`` resolver through the request's Sandbox command builder
(600-second timeout), which may start the Project's Sandbox; the resolver writes
FuseSoC build trees under the checkout's Booley Directory at
``runtime/doctor/core-resolve/<key>``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.audit.output_excerpt import output_excerpt_lines
from booley.core.boundary import (
    BoundaryError,
    require_bool,
    require_dict,
    require_list,
    require_str,
)
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.runtime import session_runtime
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle

__all__ = ["CoreResolutionRequest", "CoreResolutionResult", "inspect_core_resolution"]

# Budget for the one Sandbox command that resolves every selected Target.
_CORE_RESOLVE_TIMEOUT_S = 600
_VERDICT_MARKER = "[[CORE_RESOLVE_JSON]]"


@dataclass(frozen=True, slots=True)
class CoreResolutionRequest:
    """Inputs of the Sandbox resolution of one Project's Doctor-selected Targets."""

    project_root: Path  # the Project Root whose .core files declare the Targets
    selectors: tuple[str, ...]  # Doctor's seed Target selectors, in matrix order
    runtime_available: bool  # whether the Sandbox runtime can be entered
    sandbox_command: Callable[[list[str]], list[str]]  # lazy Sandbox entry for one argv


@dataclass(frozen=True, slots=True)
class CoreResolutionResult:
    """The graded report and the selectors whose resolution produced a verdict."""

    report: DiagnosticReport
    completed: tuple[str, ...]


def inspect_core_resolution(request: CoreResolutionRequest) -> CoreResolutionResult:
    """Resolve each selected Target in the Sandbox and grade the verdicts.

    Transitive dependencies remain covered because FuseSoC resolves the full
    dependency closure of every selected Target. Unselected vendored Targets
    are not configured merely to produce advisory notes.
    """
    findings = Findings()
    if not request.runtime_available:
        findings.skip(".core resolvability skipped - Sandbox runtime unavailable")
        return CoreResolutionResult(findings.report(), ())
    catalog = TargetCatalog.build(request.project_root)
    refs: dict[str, TargetHandle] = {}
    for selector in request.selectors:
        try:
            refs[selector] = catalog.select(selector)
        except FuseSocError as exc:
            _report_target(findings, selector, False, str(exc))
    if not refs:
        return CoreResolutionResult(findings.report(), ())
    completed = _execute(request, refs, findings)
    return CoreResolutionResult(findings.report(), completed)


def _report_target(findings: Findings, name: str, ok: bool, err: str) -> None:
    """Grade one Doctor-selected Target resolution."""
    if ok:
        findings.pass_(f".core Target '{name}' resolves")
        return
    findings.fail(f".core Target '{name}' fails to resolve", "fix the .core / depends graph")
    # A failure's excerpt is the evidence the reader needs: shown always.
    for line in output_excerpt_lines(err):
        findings.detail(line, verbose_only=False)


def _execute(
    request: CoreResolutionRequest, refs: Mapping[str, TargetHandle], findings: Findings
) -> tuple[str, ...]:
    """Run the resolver once in the Sandbox and grade what it printed."""
    payload = json.dumps(_payload(refs), separators=(",", ":"))
    try:
        command = request.sandbox_command(["python3", "-c", _CORE_RESOLVE_SNIPPET, payload])
        proc = subprocess.run(
            command,
            cwd=request.project_root,
            capture_output=True,
            text=True,
            timeout=_CORE_RESOLVE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, session_runtime.SessionError) as exc:
        findings.fail(
            f".core resolvability could not complete in the Sandbox: {exc}", "check the Sandbox"
        )
        return ()
    return _grade(proc, refs, findings)


def _grade(
    proc: subprocess.CompletedProcess[str], refs: Mapping[str, TargetHandle], findings: Findings
) -> tuple[str, ...]:
    """Require exactly one bounded verdict for every selected Target."""
    line = next(
        (line for line in reversed(proc.stdout.splitlines()) if line.startswith(_VERDICT_MARKER)),
        None,
    )
    try:
        if line is None or proc.returncode != 0:
            raise BoundaryError("missing completed Sandbox resolution verdict")
        entries = require_list(
            json.loads(line[len(_VERDICT_MARKER) :]), field="core resolution verdicts"
        )
        if len(entries) != len(refs):
            raise BoundaryError("incomplete Target resolution verdicts")
        seen: set[str] = set()
        for raw in entries:
            entry = require_dict(raw, field="core resolution")
            selector = require_str(entry, "selector")
            ok = require_bool(entry, "ok")
            if selector not in refs or selector in seen:
                raise BoundaryError("invalid Target resolution verdict")
            seen.add(selector)
            _report_target(findings, selector, ok, str(entry.get("err", "")))
        return tuple(sorted(seen))
    except (ValueError, BoundaryError):
        findings.fail(
            ".core resolvability produced incomplete Sandbox evidence",
            "check the Sandbox / FuseSoC",
        )
        return ()


# Sandbox-side resolver: resolve the host-selected Targets in one command and
# emit a machine-readable JSON verdict line the owner parses back into checks.
# Kept as a module constant so the quoting is auditable in one place.
_CORE_RESOLVE_SNIPPET = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "from booley.runtime.booley_dir import resolve_checkout_booley_dir\n"
    "from booley.fusesoc import fusesoc_registry as fr\n"
    "from booley.targets.catalog import TargetCatalog\n"
    "root = os.getcwd()\n"
    "catalog = TargetCatalog.build(root)\n"
    "targets = json.loads(sys.argv[1])\n"
    "out = []\n"
    "for target in targets:\n"
    "    selector = target['selector']\n"
    "    br = resolve_checkout_booley_dir(Path(root)) / 'runtime/doctor/core-resolve' / target['build_key']\n"
    "    try:\n"
    "        fr.resolve_target_handle(catalog.select(selector), build_root=br)\n"
    "        out.append({'selector': selector, 'ok': True})\n"
    "    except fr.FuseSocError as exc:\n"
    "        out.append({'selector': selector, 'ok': False, 'err': str(exc)})\n"
    "print('[[CORE_RESOLVE_JSON]]' + json.dumps(out))\n"
)


def _payload(refs: Mapping[str, TargetHandle]) -> list[dict[str, str]]:
    """Serialize the immutable selected Target set for the Sandbox."""
    return [
        {
            "selector": selector,
            "name": ref.name,
            "vlnv": ref.vlnv,
            "build_key": hashlib.sha256(selector.encode()).hexdigest()[:16],
        }
        for selector, ref in sorted(refs.items())
    ]
