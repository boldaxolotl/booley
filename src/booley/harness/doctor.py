"""booley doctor - setup and environment health checks.

The default doctor run is the final setup audit: host/container health,
strict project config parsing, guidance-file presence, Flow selection, and
Flow dry-runs. ``--deep`` adds real first-Target EDA smoke checks.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from booley.audit import (
    agent_schema,
    config_common,
    configs_schema,
    design_size,
    eda_environment,
    flow_schema,
    host_environment,
    project_schema,
    resource_policy,
    target_matrix,
)
from booley.config.guidance_links import (
    CANON_NAME,
    LINK_NAMES,
    ensure_guidance_links,
    guidance_entry_current,
)
from booley.config.project_config import normalize_tests_toml
from booley.core.boundary import is_str_list
from booley.dev_support.workspace_isolation import get_category_dirs
from booley.flows import execution
from booley.fusesoc import (
    core_projection,
    core_security,
    fusesoc_registry,
    selftest_overlay,
)
from booley.harness import devcontainer as dc
from booley.harness import doctor_stamp, nangate_pdk, session_runtime
from booley.harness import interactive_docker as idk
from booley.harness.colors import green, red, yellow
from booley.harness.devcontainer import (
    devcontainer_path,
    spec_mounts_token_seed,
    spec_state_is_persisted,
)
from booley.harness.doctor_waivers import (
    WAIVER_FILENAME,
    DoctorWaiverError,
    DoctorWaivers,
    DoctorWarning,
    load_doctor_waivers,
    warning,
)
from booley.harness.init_cmd import (
    DOCKER_IMAGE,
    MIN_PY,
    _detect_claude_code,
    _detect_codex,
    _docker_image_exists,
    _read_version,
    banner,
    err,
    info,
    ok,
    skip,
    warn,
)
from booley.harness.init_common import note
from booley.harness.init_docker_image import source_fingerprint_mismatch
from booley.runtime import auth_token, runtime_context
from booley.runtime import project_image as pi
from booley.runtime.git import _git_common_dir
from booley.runtime.platform_paths import docker_mount_path
from booley.runtime.project_dir import (
    PROJECT_DIR_NAME,
    resolve_checkout_project_dir,
)
from booley.runtime.timefmt import format_human_datetime
from booley.targets import target_naming
from booley.targets.flow_names import config_section
from booley.ticket_board.lifecycle import REQUIRED_BOARD_DIRS

_DOCTOR_TMP = Path("tmp") / "doctor"
_DRY_RUN_TIMEOUT_S = 60
_DEEP_TIMEOUTS_S = {
    "sim": 900,
    "lint": 300,
    "synth": 1800,
    "elab": 900,
}
# The synthesis Flow's timeout is an inner, per-target boundary. Doctor owns
# an outer subprocess that must leave time for configure work, process-tree
# cleanup, boundary interpretation, and the eager terminal report write after
# that inner boundary expires.
_SYNTH_DEEP_FINALIZE_MARGIN_S = 180

# The Booley Flows the audit loop dry-runs or deep-smokes. The EDA tool comes
# from the resolved Target; every command executes in the Session Runtime.
_AUDITED_FLOWS = ("sim", "lint", "synth")
# Flow name -> Booley Flow class path whose ``validate_execution`` classmethod
# owns that Flow's enablement/migration validation (principle 9). Elaborate
# follows [flows.sim]'s selection and validates uniformly.
_EXECUTION_VALIDATING_FLOWS = {
    "sim": ("booley.flows.sim.flow", "SimulateFlow"),
    "lint": ("booley.flows.lint.flow", "LintFlow"),
    "synth": ("booley.flows.synth.flow", "AsicSynthesizeFlow"),
    "elab": ("booley.flows.elab.flow", "ElaborateFlow"),
    "fpga": ("booley.flows.fpga.flow", "FpgaImplFlow"),
}
# --deep fail-path self-test conventions. Simulation runs its normal smoke test
# against a project-owned bad overlay; lint uses a conventional known-bad
# ``.core`` Target. See _run_selftest_checks (QA-4/QA-5).
_SELFTEST_FLOWS = ("sim", "lint")
_LINT_SELFTEST_BAD_TARGET = "lint_selftest_bad"
#: Per-Flow footprint note appended to the "fail-path unvalidated" advisory.
#: Adding a selftest is a *project-footprint* decision — a known-bad fixture
#: is a broken artifact living in someone's repo — and no doc said so, which is
#: why the fpu port hand-proved its fail paths instead (F-20). Name the cost and
#: the cheaper alternative where one exists.
_SELFTEST_FOOTPRINT_NOTE = {
    # Simulation overlays are project-owned fixtures but framework-owned
    # behavior: no Doctor-only shell belongs in booley.toml.
    "sim": (
        "Footprint: mirror only the broken staged file(s) beneath "
        ".booley_project/selftest/sim/bad-overlay/; Doctor applies that overlay "
        "only to its bad run."
    ),
    # lint has no pre_run_commands hook, so its bad Target must resolve through
    # the tracked .core -> a small known-bad source file really does land in the
    # tree. Say it plainly rather than letting the user discover it mid-setup.
    "lint": (
        "Footprint: lint's conventional 'lint_selftest_bad' must be a real .core "
        "Target - a small known-bad source file will be committed to the "
        "repo. If that footprint is unacceptable, prove the fail path by hand and "
        "record the decision; this stays a WARN, never a FAIL."
    ),
}
# Flow exit-code contract (mirror booley.dev_support.base EXIT_SUCCESS/EXIT_FAILURE):
# 0 = pass, 1 = graded design failure (fail/elab_error), 2 = infra/contract error.
_TOOL_EXIT_PASS = 0
_TOOL_EXIT_DESIGN_FAIL = 1
# HDL source suffixes used by Doctor's readmem scan. Design sizing owns its own
# source classification in :mod:`booley.audit.design_size`.
_HDL_SUFFIXES = frozenset({".v", ".sv", ".vh", ".svh"})
_CONTAINER_CLI = "doc" + "ker"
_SKILL_DIRS = (
    Path("." + "ag" + "ents") / "skills",
    Path("." + "cl" + "aude") / "skills",
)
# bwave is unconditionally required. elaborate used to be here too, but a Flow
# that is *required on the surface yet never smoke-tested* let a broken elaborate
# (a generic FuseSoC path that can't build a non-FuseSoC design) pass
# setup (QA-6). elaborate is now "validate-or-opt-out": required + deep-checked
# only when a project actually exposes it (see _elaborate_active /
# _run_elaborate_deep_check), and a legitimate opt-out (lint/simulate cover
# elaboration) is an accepted, recorded choice rather than a forced-but-unchecked
# Flow.
_BASE_REQUIRED_MCP_TOOLS = frozenset({"bwave"})
# Specialist MCP tools worth a heads-up when expected-but-absent from
# the MCP surface. An explicit ``[flows.<name>].enabled = false`` opts out.
_ADVISORY_INTERACTIVE_MCP_TOOLS = frozenset(
    {
        "mutation_tester",
        "reviewer",
    }
)
_MCP_PROBE_TIMEOUT_S = 60
_MCP_PROBE_PY = """
import json
import os
from booley.mcp import server as mcp_server

mcp_server._maybe_configure_interactive_logs_dir()
mcp_tools, errors = mcp_server._discover_booley_mcp_tools()
mcp_server._build_mcp_tool_list(mcp_tools)
names = {mcp_tool["name"] for mcp_tool in mcp_tools}
names.update(mcp_tool["name"] for mcp_tool in mcp_server._bwave_mcp_tools_for_mode())
logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
logs_dir_ok = "/.booley_project/.interactive_logs/" in logs_dir.replace("\\\\", "/")
print(json.dumps({
    "tools": sorted(names),
    "errors": errors,
    "logs_dir": logs_dir,
    "logs_dir_ok": logs_dir_ok,
}))
""".strip()

Check = Callable[[str], None]
Fail = Callable[[str, str], None]
Warn = Callable[..., None]


class _CoreAuditInputs:
    """Reuse Target source partitions within one core-audit run."""

    def __init__(
        self,
        root: Path,
        refs: dict[str, fusesoc_registry.TargetRef],
    ) -> None:
        self.refs = refs
        self._root = root
        self._sources: dict[str, fusesoc_registry.CoreSources] = {}

    def sources_for(self, name: str) -> fusesoc_registry.CoreSources:
        """Return one Target partition, reading it once per audit."""
        if name not in self._sources:
            self._sources[name] = fusesoc_registry.target_source_files_for_ref(
                self._root,
                self.refs[name],
            )
        return self._sources[name]


def _warning_sink(
    sink: Warn,
    check_id: str,
    *,
    subject: str | None = None,
    dedupe: str | None = None,
) -> Warn:
    """Bind warning metadata while preserving simple callbacks used by checks."""

    def emit(message: str, fix: str = "") -> None:
        finding = warning(check_id, str(message), subject=subject, dedupe=dedupe)
        if fix:
            sink(finding, fix)
        else:
            sink(finding)

    return emit


def _render_environment_finding(
    finding: host_environment.EnvironmentFinding,
    _pass: Check,
    _warn: Warn,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Translate a typed host finding into Doctor presentation callbacks."""
    if finding.severity is host_environment.EnvironmentSeverity.PASS:
        _pass(finding.message)
    elif finding.severity is host_environment.EnvironmentSeverity.SKIP:
        _skip(finding.message)
    elif finding.severity is host_environment.EnvironmentSeverity.FAIL:
        _fail(finding.message, finding.fix)
    else:
        assert finding.check_id is not None
        _warning_sink(_warn, finding.check_id)(finding.message, finding.fix)


@dataclass(frozen=True)
class ProjectAudit:
    """Parsed setup files needed for Doctor's Flow checks."""

    project_root: Path
    project_dir: Path
    booley_toml: dict[str, Any]
    configs_toml: dict[str, dict[str, Any]]
    first_target: str


@dataclass(frozen=True)
class DoctorFinding:
    """One structured Doctor observation, independent of console rendering."""

    severity: str
    message: str
    fix: str = ""
    check_id: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class _DoctorProfile:
    """Policy for one Doctor invocation."""

    agent_checks: bool = True

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> _DoctorProfile:
        """Resolve CLI policy once at Doctor's boundary."""
        return cls(agent_checks=not getattr(args, "skip_agent_checks", False))

    @property
    def records_health_evidence(self) -> bool:
        """Whether this complete profile may bless normal runtime health."""
        return self.agent_checks


@dataclass(frozen=True)
class DoctorRunResult:
    """Machine-readable outcome of one Doctor invocation."""

    counts: dict[str, int]
    findings: tuple[DoctorFinding, ...]
    exit_code: int
    health_evidence: bool = True

    @property
    def clean(self) -> bool:
        """True only when no active FAIL or WARN finding remains."""
        return self.counts["fail"] == 0 and self.counts["warn"] == 0


@dataclass
class _Reporter:
    """Severity tiers, strongest to weakest.

    FAIL is the only tier that reddens the summary and sets a non-zero exit
    code. WARN means "working now, but here is a real failure mode you can
    act on". NOTE is the tier below that: a true observation about a healthy
    setup with nothing to fix (design scale, for instance). The split exists
    because a WARN nobody can act on trains people to ignore the WARNs that
    name a genuine trap -- keep this tier for facts, not for muting a check
    that still has a remedy.
    """

    counts: dict[str, int]
    waivers: DoctorWaivers
    profile: _DoctorProfile
    verbose: bool = False
    _reported_warning_keys: set[tuple[str, str | None, str]] | None = None
    findings: list[DoctorFinding] | None = None

    @classmethod
    def create(
        cls,
        waivers: DoctorWaivers | None = None,
        *,
        profile: _DoctorProfile | None = None,
        verbose: bool = False,
    ) -> _Reporter:
        waiver_set = waivers or DoctorWaivers.empty(Path(WAIVER_FILENAME))
        return cls(
            counts={"pass": 0, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 0},
            waivers=waiver_set,
            profile=profile or _DoctorProfile(),
            verbose=verbose,
            _reported_warning_keys=set(),
            findings=[],
        )

    def pass_(self, msg: str) -> None:
        assert self.findings is not None
        self.findings.append(DoctorFinding("pass", str(msg)))
        ok(f"PASS  {msg}")
        self.counts["pass"] += 1

    def note_(self, msg: str, fix: str = "") -> None:
        assert self.findings is not None
        self.findings.append(DoctorFinding("note", str(msg), fix))
        note(f"NOTE  {msg}")
        if fix:
            info(f"  fix: {fix}")
        self.counts["note"] += 1

    def warn_(self, msg: str, fix: str = "") -> None:
        if not isinstance(msg, DoctorWarning):
            raise TypeError("Doctor warnings must carry a stable check ID via warning()")
        key = (msg.check_id, msg.subject, msg.dedupe or str(msg))
        assert self._reported_warning_keys is not None
        if key in self._reported_warning_keys:
            return
        self._reported_warning_keys.add(key)
        suffix = _warning_identity(msg)
        waiver = self.waivers.match(msg)
        if waiver is not None:
            assert self.findings is not None
            self.findings.append(
                DoctorFinding("waived", str(msg), waiver.reason, msg.check_id, msg.subject)
            )
            note(f"WAIVED  {msg} {suffix}")
            info(f"  reason: {waiver.reason}")
            self.counts["waived"] += 1
            return
        assert self.findings is not None
        self.findings.append(DoctorFinding("warn", str(msg), fix, msg.check_id, msg.subject))
        warn(f"WARN  {msg} {suffix}")
        if fix:
            info(f"  fix: {fix}")
        self.counts["warn"] += 1

    def skip_(self, msg: str) -> None:
        assert self.findings is not None
        self.findings.append(DoctorFinding("skip", str(msg)))
        skip(f"SKIP  {msg}")
        self.counts["skip"] += 1

    def agent_check_enabled(self, description: str) -> bool:
        """Report a profile-disabled agent check and return whether to run it."""
        if self.profile.agent_checks:
            return True
        self.skip_(f"{description} skipped by --skip-agent-checks")
        return False

    def fail_(self, msg: str, fix: str = "") -> None:
        assert self.findings is not None
        self.findings.append(DoctorFinding("fail", str(msg), fix))
        err(f"FAIL  {msg}")
        if fix:
            info(f"  fix: {fix}")
        self.counts["fail"] += 1

    def finish(self) -> int:
        for item in self.waivers.expired:
            subject = f" subject={item.subject!r}" if item.subject is not None else ""
            self.note_(
                f"expired Doctor waiver ignored: {item.check}{subject} (expired {item.expires})"
            )
        if self.verbose:
            for item in self.waivers.unused():
                subject = f" subject={item.subject!r}" if item.subject is not None else ""
                self.note_(f"Doctor waiver did not match an active warning: {item.check}{subject}")
        print()
        summary = (
            f"  {self.counts['pass']} passed, {self.counts['warn']} warning(s), "
            f"{self.counts['waived']} waived, {self.counts['note']} note(s), "
            f"{self.counts['skip']} skipped, "
            f"{self.counts['fail']} failed."
        )
        if self.counts["fail"]:
            print(red(summary))
        elif self.counts["warn"]:
            print(yellow(summary))
        else:
            print(green(summary))
        if self.counts["warn"]:
            print(
                yellow("  Resolve or explicitly waive every warning before calling setup clean.")
            )
        return 0 if self.counts["fail"] == 0 else 1

    def result(self, exit_code: int) -> DoctorRunResult:
        """Freeze the accumulated findings after :meth:`finish`."""
        assert self.findings is not None
        return DoctorRunResult(
            dict(self.counts),
            tuple(self.findings),
            exit_code,
            health_evidence=self.profile.records_health_evidence,
        )


def _warning_identity(finding: DoctorWarning) -> str:
    subject = f":{finding.subject}" if finding.subject is not None else ""
    return f"[{finding.check_id}{subject}]"


def _load_waivers(project_root: Path) -> tuple[DoctorWaivers, str | None]:
    """Resolve and load project waivers without hiding config failures."""

    try:
        project_dir = resolve_checkout_project_dir(project_root)
    except FileNotFoundError:
        project_dir = project_root / PROJECT_DIR_NAME
    try:
        return load_doctor_waivers(project_dir), None
    except DoctorWaiverError as exc:
        return DoctorWaivers.empty(project_dir / WAIVER_FILENAME), str(exc)


def _create_reporter(
    project_root: Path,
    *,
    profile: _DoctorProfile,
    verbose: bool,
) -> _Reporter:
    waivers, waiver_error = _load_waivers(project_root)
    reporter = _Reporter.create(waivers, profile=profile, verbose=verbose)
    if waiver_error:
        reporter.fail_(
            f"Doctor waiver file invalid: {waiver_error}",
            f"fix or remove {waivers.path}",
        )
    return reporter


def run_doctor(args: argparse.Namespace, project_root: Path) -> int:
    """Run setup health checks."""
    result = run_doctor_result(args, project_root)
    if runtime_context.inside_session_runtime():
        try:
            from booley.harness.auto_doctor import record_manual_result

            record_manual_result(project_root, result)
        except Exception:  # noqa: BLE001 — advisory state must never change manual Doctor's result
            pass
    return result.exit_code


def run_doctor_result(
    args: argparse.Namespace,
    project_root: Path,
    *,
    read_only: bool = False,
    record_clean: bool = True,
) -> DoctorRunResult:
    """Run Doctor and return its structured result.

    ``read_only`` is the automatic-health profile: it reports repairable
    conditions but never rewrites guidance links or moves orphaned tickets.
    Manual ``booley doctor`` retains its established self-healing behavior.
    """
    banner("Booley Doctor")
    info(f"version: {_read_version()}")
    print()

    verbose = getattr(args, "verbose", False)
    deep = getattr(args, "deep", False)
    profile = _DoctorProfile.from_args(args)
    reporter = _create_reporter(project_root, profile=profile, verbose=verbose)
    docker_exe, project = _run_project_phase(project_root, reporter, read_only=read_only)
    _run_runtime_phase(project, docker_exe, verbose, reporter)
    _run_flow_and_core_phase(project, docker_exe, verbose, reporter)

    if deep:
        _run_deep_phase(project, docker_exe, verbose, reporter)

    result = reporter.result(reporter.finish())
    if project is not None and result.clean and result.health_evidence and record_clean:
        doctor_stamp.record_clean_run(project.project_dir, project_root, deep=deep)
    return result


def _run_project_phase(
    project_root: Path,
    reporter: _Reporter,
    *,
    read_only: bool,
) -> tuple[str | None, ProjectAudit | None]:
    """Run host, config, Git, and Ticket Board checks."""
    banner("Host checks")
    docker_exe = _run_host_checks(reporter.pass_, reporter.warn_, reporter.skip_, reporter.fail_)
    project = _check_project_setup(project_root, reporter.pass_, reporter.warn_, reporter.fail_)
    if project is None:
        reporter.skip_("project setup audit skipped - no valid project config")
    else:
        _check_agents_md(
            project,
            reporter.pass_,
            reporter.warn_,
            _note=reporter.note_,
            repair=not read_only,
        )
    _check_worktree_prune_guard(project_root, reporter.pass_, reporter.skip_, reporter.fail_)
    _check_line_endings(
        project_root, reporter.pass_, reporter.warn_, reporter.skip_, reporter.fail_
    )
    if project is not None:
        _check_worktree_core_shadow_guard(project.project_dir, reporter.pass_, reporter.warn_)
        stealth = project.booley_toml.get("stealth")
        explicit_stealth = isinstance(stealth, Mapping) and stealth.get("enabled") is True
        _check_stealth_cores(
            project_root,
            project.project_dir,
            reporter.pass_,
            reporter.fail_,
            stealth_enabled=explicit_stealth,
            repair=not read_only,
        )
    _check_board_orphans(
        project_root,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        repair=not read_only,
    )
    return docker_exe, project


def _run_runtime_phase(
    project: ProjectAudit | None,
    docker_exe: str | None,
    verbose: bool,
    reporter: _Reporter,
) -> None:
    """Run runtime-location, container, MCP, and preflight-parity checks."""
    sandbox_image = _sandbox_image(project)
    _check_runtime_location(
        docker_exe,
        sandbox_image,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
    )
    _check_memory_invariant(project, reporter.pass_, reporter.warn_, reporter.skip_)
    _run_container_checks(
        project,
        docker_exe,
        sandbox_image,
        verbose,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
    )
    _run_mcp_checks(
        project,
        docker_exe,
        sandbox_image,
        verbose,
        reporter,
    )
    _run_preflight_parity_checks(project, reporter)


def _run_flow_and_core_phase(
    project: ProjectAudit | None,
    docker_exe: str | None,
    verbose: bool,
    reporter: _Reporter,
) -> None:
    """Run Flow dry-runs and the authored FuseSoC core audit."""
    banner("Booley Flow setup checks")
    if project is None:
        reporter.skip_("Flow dry-runs skipped - project config invalid")
    else:
        _run_flow_audit(
            project,
            docker_exe,
            verbose,
            reporter.pass_,
            reporter.note_,
            reporter.warn_,
            reporter.skip_,
            reporter.fail_,
        )
    banner("FuseSoC .core checks")
    if project is None:
        reporter.skip_(".core audit skipped - project config invalid")
    else:
        _run_core_audit(
            project,
            reporter.pass_,
            reporter.warn_,
            reporter.skip_,
            reporter.fail_,
            _note=reporter.note_,
        )


def _run_deep_phase(
    project: ProjectAudit | None,
    docker_exe: str | None,
    verbose: bool,
    reporter: _Reporter,
) -> None:
    banner("Deep checks")
    if project is None:
        reporter.skip_("deep checks skipped - project config invalid")
        return
    # First, before the EDA smoke checks: the probe's RUSAGE_CHILDREN reading
    # is exact only while no bigger child (a real sim/synth run) has been
    # reaped yet.
    if reporter.agent_check_enabled("developer authorization probe"):
        _run_developer_probe(project, reporter.pass_, reporter.skip_, reporter.fail_)
    _run_deep_checks(
        project,
        docker_exe,
        verbose,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
    )
    _run_core_resolve_checks(
        project,
        docker_exe,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _note=reporter.note_,
    )


def _run_host_checks(_pass: Check, _warn: Check, _skip: Check, _fail: Fail) -> str | None:
    """Run host environment checks. Returns the container CLI path."""
    py_ver = sys.version_info
    _render_environment_finding(
        host_environment.audit_python_version((py_ver.major, py_ver.minor), MIN_PY),
        _pass,
        _warn,
        _skip,
        _fail,
    )

    try:
        import booley

        _pass(f"booley package v{booley.__version__}")
        _check_legacy_distribution(_pass, _fail)
    except ImportError:
        _fail("booley package not importable", "pip install booley-rtl")

    # _check_docker SKIPs itself inside the Session Runtime (QA-3, ADR 0028).
    docker_exe = _check_docker(_pass, _skip, _fail)

    from booley.runtime import runtime_context

    # The host-clock check is a host-only concern (sandbox image builds run on
    # the host); skip it in-container where there is nothing to build (F-5).
    if not runtime_context.inside_session_runtime():
        _check_host_clock(_pass, _warn, _skip)
    _check_skills(_pass, _warn, _fail)
    return docker_exe


def _check_legacy_distribution(_pass: Check, _fail: Fail) -> None:
    """Render the extracted legacy-distribution audit."""
    _render_environment_finding(
        host_environment.audit_legacy_distribution(),
        _pass,
        lambda _message: None,
        lambda _message: None,
        _fail,
    )


#: ``.core`` security violations whose verdict depends on the agent's write
#: Scope. doctor only has a synthetic project-wide Scope, so these are
#: advisory there; the per-ticket Scope check at commit time is the real gate.
#: Everything else (``fpga_hook``, ``expr_param``) is a property of the
#: ``.core`` itself and stays a hard FAIL.
_SCOPE_DEPENDENT_VIOLATIONS = frozenset({"in_scope_script", "unconfinable_script"})


def _check_host_clock(_pass: Check, _warn: Check, _skip: Check) -> None:
    """Render the extracted host clock probe."""
    _render_environment_finding(
        host_environment.probe_host_clock(), _pass, _warn, _skip, lambda *_args: None
    )


def _check_docker(_pass: Check, _skip: Check, _fail: Fail) -> str | None:
    """Render the extracted container-runtime probe."""
    from booley.runtime import runtime_context

    audit = host_environment.probe_container_runtime(
        _CONTAINER_CLI,
        inside_session_runtime=runtime_context.inside_session_runtime(),
        which=shutil.which,
        run=subprocess.run,
    )
    _render_environment_finding(audit.finding, _pass, lambda _message: None, _skip, _fail)
    return audit.executable


def _docker_permission_denied_fix() -> str:
    """Compatibility facade for the extracted runtime permission guidance."""
    return host_environment.docker_permission_denied_fix()


def _sandbox_image(project: ProjectAudit | None) -> str:
    """Return configured sandbox image, falling back to the base image."""
    if project is None:
        return DOCKER_IMAGE
    raw = project.booley_toml.get("sandbox", {}).get("image", "")
    if isinstance(raw, str) and raw.strip():
        return raw
    if (project.project_dir / "docker" / "Dockerfile").is_file():
        from booley.runtime.project_image import project_image_name

        return project_image_name(project.project_root)
    return DOCKER_IMAGE


def _docker_image_exists_by_name(image: str) -> bool:
    """Return whether *image* is available locally."""
    if image == DOCKER_IMAGE:
        return _docker_image_exists()
    docker_exe = shutil.which(_CONTAINER_CLI)
    if not docker_exe:
        return False
    try:
        result = subprocess.run(
            [docker_exe, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _image_env_value(docker_exe: str, image: str, key: str) -> str | None:
    """Return the baked value of env var *key* in *image*, or None if unset.

    Reads ``.Config.Env`` via ``docker image inspect`` (same source as the runtime
    marker check). None also covers every "can't tell" case — inspect failure,
    missing image, malformed output.
    """
    try:
        result = subprocess.run(
            [docker_exe, "image", "inspect", image, "--format", "{{json .Config.Env}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        env_list = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(env_list, list):
        return None
    prefix = f"{key}="
    for entry in env_list:
        if isinstance(entry, str) and entry.startswith(prefix):
            return entry[len(prefix) :]
    return None


def _check_project_setup(
    project_root: Path,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
) -> ProjectAudit | None:
    """Strictly parse and validate Booley project setup files."""
    try:
        project_dir = resolve_checkout_project_dir(project_root)
    except FileNotFoundError:
        _fail("project directory not found", "booley init")
        return None

    _pass(f"project directory found: {project_dir}")

    booley_toml = _load_toml(project_dir / "booley.toml", _pass, _fail)
    if booley_toml is None:
        return None

    valid = _validate_booley_toml(booley_toml, project_dir, _pass, _warn, _fail)
    from booley.targets.flow_names import canonicalize_config

    booley_toml = canonicalize_config(booley_toml)

    # configs.toml is optional now: ADR 0022 makes the ``.core`` the sole
    # design-description home, and the legacy configs.toml registry path was
    # removed. Validate a configs.toml only when a project still ships one.
    configs_toml: dict[str, dict[str, Any]] = {}
    configs_path = project_dir / "configs.toml"
    if configs_path.is_file():
        configs_raw = _load_toml(configs_path, _pass, _fail)
        if configs_raw is None:
            return None
        validated = _validate_configs_toml(configs_raw, _pass, _fail)
        if validated is None:
            valid = False
        else:
            configs_toml = validated

    # Deep-check config selection comes from the .core Targets (the design home);
    # fall back to a configs.toml config name only if no .core is authored.
    first_target = ""
    try:
        from booley.fusesoc import fusesoc_registry

        targets = fusesoc_registry.available_targets(project_root)
        first_target = targets[0] if targets else ""
    except Exception:  # noqa: BLE001 — registry may be unavailable; fall back to a configs.toml config name
        first_target = ""
    if not first_target:
        first_target = next(iter(configs_toml), "")

    if not valid:
        return None

    if first_target:
        _pass(f"first deep-check config: {first_target}")
    return ProjectAudit(
        project_root=project_root,
        project_dir=project_dir,
        booley_toml=booley_toml,
        configs_toml=configs_toml,
        first_target=first_target,
    )


def _load_toml(path: Path, _pass: Check, _fail: Fail) -> dict[str, Any] | None:
    if not path.is_file():
        _fail(f"{path.name} missing", "booley init")
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        _fail(f"{path.name} does not parse: {exc}", f"fix {path}")
        return None
    except OSError as exc:
        _fail(f"{path.name} unreadable: {exc}", f"check permissions on {path}")
        return None
    _pass(f"{path.name} parses")
    return data


def _validate_booley_toml(
    data: dict[str, Any],
    project_dir: Path,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
) -> bool:
    """Validate the project-level booley.toml schema used by doctor."""
    if not _render_config_audit(project_schema.audit_eda_config(data), _pass, _warn, _fail):
        return False
    valid = _render_config_audit(project_schema.audit_project_table(data), _pass, _warn, _fail)
    valid &= _render_config_audit(agent_schema.audit_agent_table(data), _pass, _warn, _fail)
    valid &= _render_config_audit(agent_schema.audit_models_table(data), _pass, _warn, _fail)
    valid &= _render_config_audit(project_schema.audit_feedback_table(data), _pass, _warn, _fail)
    valid &= _render_config_audit(project_schema.audit_stealth_table(data), _pass, _warn, _fail)
    valid &= _render_config_audit(
        flow_schema.audit_flow_tables(data, _AUDITED_FLOWS), _pass, _warn, _fail
    )
    valid &= _render_config_audit(project_schema.audit_sandbox_table(data), _pass, _warn, _fail)
    valid &= _render_config_audit(
        project_schema.audit_interactive_table(data), _pass, _warn, _fail
    )
    valid &= _render_config_audit(project_schema.audit_developer_table(data), _pass, _warn, _fail)
    _render_config_audit(project_schema.audit_known_tables(data), _pass, _warn, _fail)
    # Source RTL/TB layout is validated from the .core tags:[tb] partition (see
    # _run_core_checks → sim_target_has_untagged_tb), not a booley.toml
    # [sources.*] table — those fields were retired (ADR 0026 follow-through).

    if not project_dir.exists():
        _fail(f"project directory missing: {project_dir}", "booley init")
        valid = False
    return valid


def _render_config_audit(
    audit: config_common.ConfigTableAudit,
    _pass: Check,
    _warn: Warn,
    _fail: Fail,
) -> bool:
    """Translate domain findings into Doctor's presentation callbacks."""
    for finding in audit.findings:
        if finding.severity is config_common.ConfigFindingSeverity.PASS:
            _pass(finding.message)
        elif finding.severity is config_common.ConfigFindingSeverity.FAIL:
            _fail(finding.message, finding.fix)
        else:
            assert finding.check_id is not None
            _warning_sink(_warn, finding.check_id, subject=finding.subject)(
                finding.message, finding.fix
            )
    return audit.is_valid


def _validate_agent_table(data: dict[str, Any], _pass: Check, _fail: Fail) -> bool:
    """Compatibility facade for the extracted agent configuration audit."""
    return _render_config_audit(
        agent_schema.audit_agent_table(data), _pass, lambda _message: None, _fail
    )


def _validate_models_table(data: dict[str, Any], _pass: Check, _warn: Check, _fail: Fail) -> bool:
    """Compatibility facade for the extracted models configuration audit."""
    return _render_config_audit(agent_schema.audit_models_table(data), _pass, _warn, _fail)


def _validate_flow_tables(data: dict[str, Any], _warn: Check, _fail: Fail) -> bool:
    """Compatibility facade for the extracted Flow configuration audit."""
    return _render_config_audit(
        flow_schema.audit_flow_tables(data, _AUDITED_FLOWS),
        lambda _message: None,
        _warn,
        _fail,
    )


def _validate_one_flow_table(
    flow_name: str,
    section: Any,
    _warn: Check,
    _fail: Fail,
) -> bool:
    """Compatibility facade for one extracted Flow-table audit."""
    return _render_config_audit(
        flow_schema.audit_flow_table(flow_name, section),
        lambda _message: None,
        _warn,
        _fail,
    )


def _validate_configs_toml(
    raw: dict[str, Any],
    _pass: Check,
    _fail: Fail,
) -> dict[str, dict[str, Any]] | None:
    audit = configs_schema.audit_configs_toml(raw)
    for issue in audit.issues:
        _fail(issue.message, issue.fix)
    if audit.configs is None:
        return None
    _pass(f"configs.toml contains {len(audit.configs)} valid config(s)")
    return audit.configs


def _check_agents_md(
    project: ProjectAudit,
    _pass: Check,
    _warn: Check,
    *,
    _note: Check | None = None,
    repair: bool = True,
) -> None:
    """Check the canonical guidance file and ensure root links point to it.

    The canonical AGENTS.md lives in the project data dir; the RTL repo root
    only carries generated AGENTS.md/CLAUDE.md links to it.
    """
    note_sink = _note or _pass
    canon = project.project_dir / CANON_NAME
    if not canon.is_file():
        note_sink("project guidance file missing; run setup guidance when ready")
        return
    link_warn = _warning_sink(_warn, "guidance.links-unhealthy")
    if repair:
        try:
            ensure_guidance_links(project.project_root, project.project_dir)
            _pass("project guidance file present; root links ensured")
        except OSError as exc:
            link_warn(f"could not create root guidance links: {exc}")
    elif _guidance_links_current(project.project_root, canon):
        _pass("project guidance file present; root links current")
    else:
        link_warn(
            "project guidance root links are missing or stale",
            "run `booley doctor` to repair AGENTS.md and CLAUDE.md",
        )
    _check_guidance_runtime_note(canon, _pass, _warn)


def _guidance_links_current(project_root: Path, canon: Path) -> bool:
    """True when every root entry is a live link or matching tracked file."""
    try:
        resolved_canon = canon.resolve(strict=True)
    except OSError:
        return False
    for name in LINK_NAMES:
        link = project_root / name
        try:
            if not guidance_entry_current(project_root, link, resolved_canon):
                return False
        except OSError:
            return False
    return True


# The guidance file is read by host-side agent sessions too — the root
# CLAUDE.md link resolves on both host and runtime by design (guidance_links), but the
# Booley Flows it names exist only inside the Session Runtime. Guidance that names
# them without naming the runtime location hands a host agent instructions it cannot
# satisfy; AGENTS_TEMPLATE.md carries the scoping note, so projects written
# before it (or edited since) need telling.
_GUIDANCE_BTOOL_MARKER = "booley_status"
_GUIDANCE_RUNTIME_MARKER = "session runtime"


def _check_guidance_runtime_note(canon: Path, _pass: Check, _warn: Check) -> None:
    """The guidance must scope its Booley Flow instructions to the Session Runtime."""
    _warn = _warning_sink(_warn, "guidance.session-runtime-scope", subject=str(canon))
    try:
        text = canon.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as exc:
        _warn(f"could not read {canon}: {exc}")
        return
    if _GUIDANCE_BTOOL_MARKER not in text:
        return  # no Booley Flow instructions to scope
    if _GUIDANCE_RUNTIME_MARKER in text:
        _pass("project guidance scopes Booley Flows to the Session Runtime")
        return
    _warn(
        "project guidance tells agents to call booley_status and the Booley Flows but never says "
        "they exist only inside the Session Runtime — a host-side agent session sees no such "
        "Booley Flows and falls back to raw EDA commands",
        f"add the Session Runtime scoping bullet from AGENTS_TEMPLATE.md to {canon}",
    )


def _check_worktree_prune_guard(
    project_root: Path,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """ADR 0028 Decision 10: host ``git gc`` must never prune ticket worktrees.

    Worktrees are created in-container, so their git metadata records
    container paths the host cannot see; without ``gc.worktreePruneExpire=
    never`` a host-side ``git gc`` silently drops those registrations.
    ``booley init`` sets the knob; this check catches repos initialized
    before ADR 0028 (or a user resetting their git config).
    """
    from booley.harness.init_git_hooks import (
        WORKTREE_PRUNE_KEY,
        WORKTREE_PRUNE_VALUE,
    )

    try:
        probe = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if probe.returncode != 0:
            _skip("worktree prune guard: project root is not a git repo")
            return
        got = subprocess.run(
            ["git", "-C", str(project_root), "config", "--get", WORKTREE_PRUNE_KEY],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        value = got.stdout.strip() if got.returncode == 0 else None
    except (FileNotFoundError, subprocess.SubprocessError):
        _skip("worktree prune guard: git unavailable")
        return

    if value == WORKTREE_PRUNE_VALUE:
        _pass(
            f"{WORKTREE_PRUNE_KEY}={WORKTREE_PRUNE_VALUE} "
            "(in-container worktrees safe from host git gc)"
        )
        return
    detail = f"set to {value!r}" if value else "unset"
    _fail(
        f"{WORKTREE_PRUNE_KEY} is {detail} — a host-side `git gc` can prune "
        "in-container ticket worktree registrations",
        f"git -C {project_root} config {WORKTREE_PRUNE_KEY} {WORKTREE_PRUNE_VALUE}",
    )


def _check_line_endings(
    project_root: Path,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Catch a CRLF working tree the container would read as fully modified.

    ``booley init`` sets ``core.autocrlf=false`` and commits the tree to LF, but
    that can drift back: a git config reset, or a fresh clone on a Windows box
    whose ``.gitattributes`` never carried the rule. Ticket Mode is what breaks
    — phantom in-container diffs trip the dirty-tree check, scope enforcement,
    and worktrees — so this is worth re-asking every run, not only at init.

    Reports a *present* problem only. CRLF on disk FAILs (Ticket Mode is broken
    now); ``autocrlf=true`` with a clean tree WARNs (the next checkout will
    break it). A missing ``.gitattributes`` rule is deliberately silent: it is
    harmless on the host doing the asking, and most vendored upstream repos
    (the pristine picorv32 among them) will never carry one — flagging it would
    be the unfollowable advice :func:`_owned_core_files` exists to avoid.
    """
    _warn = _warning_sink(_warn, "git.autocrlf-risk")

    from booley.harness.init_git_hooks import _count_crlf_worktree_files

    try:
        probe = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if probe.returncode != 0:
            _skip("line endings: project root is not a git repo")
            return
        got = subprocess.run(
            ["git", "-C", str(project_root), "config", "--get", "core.autocrlf"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        _skip("line endings: git unavailable")
        return

    autocrlf = got.stdout.strip().lower() if got.returncode == 0 else ""
    crlf_count = _count_crlf_worktree_files(project_root)
    if crlf_count is None:
        _skip("line endings: could not read `git ls-files --eol`")
        return

    if crlf_count:
        _fail(
            f"{crlf_count} tracked file(s) are checked out with CRLF — the Session "
            "Runtime container sees every one as modified, which breaks the "
            "dirty-tree check, scope enforcement, and ticket worktrees",
            "booley init --fix-line-endings   (from a clean tree)",
        )
        return
    if autocrlf == "true":
        _warn(
            "core.autocrlf=true — the tree is LF today, but the next clone or "
            "checkout will re-create it with CRLF and break Ticket Mode",
            f"git -C {project_root} config core.autocrlf false   (or re-run `booley init`)",
        )
        return
    _pass("working tree is container-safe (no CRLF checkouts, autocrlf off)")


def _check_worktree_core_shadow_guard(
    project_dir: Path,
    _pass: Check,
    _warn: Check,
) -> None:
    """Ensure ``.booley_project/FUSESOC_IGNORE`` keeps worktree cores out of scan.

    Per-ticket / baseline git worktrees under ``.booley_project/`` are full
    checkouts carrying COPIES of the project's ``.core`` files (same VLNV). A
    stale copy shadows the repo-root source in FuseSoC's recursive
    ``--cores-root`` scan — silently building the worktree's RTL, not ``/work``'s.
    ``booley init`` and ``worktree_create.sh`` drop the marker that suppresses
    this; catch a project that predates the fix (or whose marker was removed),
    escalating the message when a live shadowing ``.core`` is already present.
    """
    _warn = _warning_sink(_warn, "fusesoc.worktree-core-shadow")
    marker = project_dir / "FUSESOC_IGNORE"
    if marker.is_file():
        _pass("FUSESOC_IGNORE present (worktree .core copies can't shadow repo-root source)")
        return
    # Marker absent — is there an actual stale worktree .core to shadow right now?
    # Authored stealth cores (.booley_project/cores/, ADR 0036) are sources, not
    # shadow threats — they are scanned deliberately and must not be counted.
    stealth_root = project_dir / fusesoc_registry.STATE_CORES_SUBDIR
    shadow_cores = [
        p
        for sub in (project_dir / "worktrees", project_dir)
        if sub.is_dir()
        for p in sub.rglob("*.core")
        if not p.is_relative_to(stealth_root)
    ]
    if shadow_cores:
        _warn(
            f".booley_project/FUSESOC_IGNORE missing and {len(shadow_cores)} "
            "worktree .core copy/copies can shadow the repo-root source in "
            "FuseSoC's --cores-root scan; run `booley init` to add the marker"
        )
    else:
        _warn(
            ".booley_project/FUSESOC_IGNORE missing; a future ticket worktree's "
            ".core could shadow the repo-root source — run `booley init`"
        )


# State-dir subtrees that legitimately hold transient .core COPIES (worktree /
# baseline checkouts, build caches) — never authored sources, never "stranded".
_STATE_TRANSIENT_DIR_NAMES = frozenset({"worktrees", "build", "_build", ".runtime", ".git", "tmp"})


def _check_stealth_cores(
    project_root: Path,
    project_dir: Path,
    _pass: Check,
    _fail: Fail,
    *,
    stealth_enabled: bool | None = None,
    repair: bool = False,
) -> None:
    """Audit authored stealth cores and their root projections.

    Two failure modes, each of which silently empties or corrupts the Target
    registry when left undiagnosed:

    * an authored ``.core`` stranded in the state dir OUTSIDE
      ``.booley_project/cores/`` — :func:`~booley.fusesoc.fusesoc_registry.discover_cores`
      skips the state tree structurally, so the core just vanishes from the
      selectable Targets (``"(none authored)"`` with no hint why);
    * the same logical VLNV authored in both the repo tree and
      ``.booley_project/cores/`` — enumeration hard-errors on this
      (:class:`~booley.fusesoc.fusesoc_registry.CoreCollisionError`), and doctor turns
      that into an actionable finding before an MCP tool call trips over it.
    """
    stealth_root = project_dir / fusesoc_registry.STATE_CORES_SUBDIR
    authored = tuple(sorted(stealth_root.rglob("*.core"))) if stealth_root.is_dir() else ()
    if stealth_enabled is False and authored:
        _fail(
            ".booley_project/cores contains authored cores while stealth mode is disabled",
            "set [stealth] enabled = true, or move the cores into the tracked repository",
        )
    elif stealth_enabled is True:
        _check_core_projections(project_root, _pass, _fail, repair=repair)

    stranded = [
        p
        for p in project_dir.rglob("*.core")
        if not p.is_relative_to(stealth_root)
        and not _STATE_TRANSIENT_DIR_NAMES.intersection(p.relative_to(project_dir).parts)
        and not any(part.startswith(".baseline-wt") for part in p.relative_to(project_dir).parts)
        and fusesoc_registry.TRACE_OVERLAY_MARKER not in p.name
    ]
    if stranded:
        names = ", ".join(str(p.relative_to(project_dir)) for p in sorted(stranded))
        _fail(
            f"authored .core stranded in the state dir, invisible to Booley and FuseSoC: {names}",
            f"move it under {stealth_root} — the one scanned subtree of .booley_project/ (ADR 0036)",
        )
    else:
        _pass("no authored .core stranded outside .booley_project/cores/")

    try:
        fusesoc_registry.enumerate_targets(project_root)
    except fusesoc_registry.CoreCollisionError as exc:
        _fail(str(exc), "rename or delete one of the colliding cores")
    except fusesoc_registry.FuseSocError:
        # Unreadable/misnamed cores are the core-audit checks' beat, not ours.
        return
    else:
        _pass("no VLNV collision between repo-tree and .booley_project/cores/ cores")


def _check_core_projections(
    project_root: Path,
    _pass: Check,
    _fail: Fail,
    *,
    repair: bool,
) -> None:
    """Audit or reconcile the derived root-level core copies."""
    failed = False
    if repair:
        try:
            core_projection.reconcile_projected_cores(project_root)
        except (core_projection.CoreProjectionError, OSError) as exc:
            _fail(f"stealth core projection failed: {exc}", "fix the conflict and run booley init")
            failed = True
    try:
        issues = core_projection.projection_issues(project_root)
    except (core_projection.CoreProjectionError, OSError) as exc:
        _fail(
            f"could not inspect stealth core projections: {exc}",
            "fix the core and run booley init",
        )
        failed = True
        issues = ()
    if issues:
        _fail(
            f"stealth core projections are not current: {', '.join(issues)}",
            "run booley init to reconcile the ignored root-level projections",
        )
    elif not failed:
        _pass("stealth core projections match .booley_project/cores/")
    _check_projection_exclude(project_root, _pass, _fail)


def _check_projection_exclude(project_root: Path, _pass: Check, _fail: Fail) -> None:
    """Require generated core projections to stay out of host git status."""
    common = _git_common_dir(project_root)
    if common is None:
        return
    exclude = common / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    pattern = f"/{core_projection.PROJECTED_CORE_GLOB}"
    if pattern not in lines:
        _fail(
            "stealth core projections are not excluded from host git status",
            "run booley init to add the generated projection pattern to .git/info/exclude",
        )
    else:
        _pass("stealth core projections are excluded through .git/info/exclude")


def _check_board_orphans(
    project_root: Path,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    *,
    repair: bool = True,
) -> None:
    """ADR 0028 Decision 11: Ticket Board self-heal for dead developer PIDs.

    A ticket stuck in active/ whose owning PID is dead (crashed ``booley
    run``, killed container process) is recovered — blocked with a note for
    triage — by the same startup sweep every ``booley run`` performs. PIDs
    are container-scoped under ADR 0028, so the sweep is only meaningful when
    doctor itself runs inside the Session Runtime; host-side it is skipped
    (a container PID checked from the host is a different namespace).
    """
    _warn = _warning_sink(_warn, "tickets.orphan-recovered")

    from booley.runtime import runtime_context

    if not runtime_context.inside_session_runtime():
        _skip("board orphan self-heal: runs in-container (ticket PIDs are container-scoped)")
        return

    # Lazy imports: booley.harness.booley imports this module at load time.
    from booley.harness.booley import get_active_slugs
    from booley.harness.orphan_handler import find_startup_orphans, handle_startup_orphans

    active = get_active_slugs(project_root)
    if not active:
        _pass("no tickets in active/ — no orphans to recover")
        return

    if not repair:
        orphans = find_startup_orphans(project_root)
        if orphans:
            names = ", ".join(orphans[:3])
            suffix = f" and {len(orphans) - 3} more" if len(orphans) > 3 else ""
            _warn(
                f"found {len(orphans)} orphaned ticket(s) in active/: {names}{suffix}",
                "run `booley run` or manual `booley doctor` to recover them for triage",
            )
        else:
            _pass(f"{len(active)} active ticket(s), all with live owner PIDs")
        return

    recovered = handle_startup_orphans(project_root)
    if recovered:
        _warn(
            f"recovered {recovered} orphaned ticket(s) from active/ "
            "(dead developer PID) — blocked with a note for triage"
        )
    else:
        _pass(f"{len(active)} active ticket(s), all with live owner PIDs")


# ---------------------------------------------------------------------------
# ADR 0028 Decision 12: container memory invariant + runtime detection health
# ---------------------------------------------------------------------------

# Actionable fix shared by both runtime-marker failures: the marker is baked by
# the sandbox image build, and the derived-image drift gotcha means the base
# rebuild alone silently leaves per-project images stale.
_RUNTIME_MARKER_FIX = (
    "rebuild the base AND derived sandbox images (booley init --force) — "
    "the image predates the ADR 0028 runtime marker"
)


def _developer_mem_bytes(project_dir: Path) -> tuple[int, bool]:
    """(developer memory term, measured?) for the invariant arithmetic.

    Uses the peak RSS ``doctor --deep`` recorded into runtime state when
    present, else the ADR 0028 1 GiB fallback.
    """
    from booley.harness import developer_probe

    measured = developer_probe.load_measurement(project_dir)
    if measured is not None:
        return (measured, True)
    return (developer_probe.FALLBACK_BYTES, False)


def _heavy_memory_reservation(project: ProjectAudit) -> resource_policy.HeavyMemoryReservation:
    """Load project evidence and apply the extracted HEAVY-memory policy."""
    jobs = project.booley_toml.get("jobs", {})
    raw = jobs.get("heavy_memory") if isinstance(jobs, dict) else None
    from booley.harness import synth_probe

    measurement = synth_probe.load_measurement(project.project_dir)
    calibration = (
        resource_policy.SynthesisMemoryCalibration(
            target=str(measurement["target"]),
            peak_rss_bytes=int(measurement["peak_rss_bytes"]),
        )
        if measurement is not None
        else None
    )
    return resource_policy.heavy_memory_reservation(
        raw,
        calibration,
        target_matrix.doctor_targets(project.project_root, "synth"),
    )


def _check_memory_invariant(
    project: ProjectAudit | None,
    _pass: Check,
    _warn: Check,
    _skip: Check,
) -> None:
    """ADR 0028 Decision 12 (advisory): the one container fits its caps.

    Container-only means every ticket's Developer Agent and every HEAVY EDA job
    share one cgroup — a limit sized below what the ``[jobs]`` caps admit is
    an OOM waiting to take the whole session down. WARN, never FAIL: the
    budget terms are estimates, and an over-committed container still works
    until the jobs actually coincide.
    """
    _warn = _warning_sink(_warn, "sandbox.memory-overcommit")

    from booley.runtime import job_slots

    if project is None:
        _skip("memory invariant skipped - no valid project config")
        return

    if runtime_context.inside_session_runtime():
        limit = resource_policy.cgroup_memory_limit_bytes()
        if limit is None:
            _pass(
                "memory invariant: container memory is unlimited "
                "(no cgroup limit) — nothing to enforce"
            )
            return
        source = "container memory"
    else:
        mem_str = resource_policy.configured_sandbox_memory(project.booley_toml)
        if not mem_str:
            _skip("memory invariant: no [sandbox] memory limit configured — nothing to check")
            return
        limit = resource_policy.parse_memory_limit(mem_str)
        if limit is None:
            _warn(
                f"[sandbox] memory = {mem_str!r} is unparseable — "
                "cannot check the memory invariant"
            )
            return
        source = "[sandbox] memory"

    caps = job_slots.parse_caps(project.booley_toml)
    reservation = _heavy_memory_reservation(project)
    if reservation.error:
        _warn(reservation.error)
        return
    orch, measured = _developer_mem_bytes(project.project_dir)
    requirement = resource_policy.memory_requirement(
        max_heavy=caps.max_heavy,
        heavy_job_bytes=reservation.bytes,
        max_tickets=caps.max_tickets,
        developer_bytes=orch,
    )
    fmt = resource_policy.format_memory
    arithmetic = (
        f"{caps.max_heavy}x{fmt(reservation.bytes)} + {caps.max_tickets}x{fmt(orch)} "
        f"+ 2g = {fmt(requirement.required_bytes)}"
    )
    orch_note = (
        "measured developer RSS"
        if measured
        else "1g developer fallback — doctor --deep measures it"
    )
    if limit >= requirement.required_bytes:
        _pass(
            f"memory invariant holds: {source} {fmt(limit)} ≥ {arithmetic} "
            f"({reservation.evidence}; {orch_note})"
        )
    else:
        _warn(
            f"{source} {fmt(limit)} < {arithmetic} — raise the "
            "devcontainer memory or lower [jobs] caps; set [jobs].heavy_memory "
            f"to record the calibrated reservation ({reservation.evidence}; {orch_note})"
        )


def _check_runtime_location(
    docker_exe: str | None,
    sandbox_image: str,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """ADR 0028: runtime detection must be authoritative on both sides.

    In-container, ``BOOLEY_CONTAINER=1`` (baked by the sandbox image) is the
    contract — detection through the ``/.dockerenv`` fallback still works but
    means a stale pre-ADR-0028 image; and the shared slot store every process
    arbitrates admission through must be writable. Host-side, verify the
    sandbox image bakes the marker so the container sessions it spawns detect
    correctly.
    """
    _warn = _warning_sink(_warn, "sandbox.runtime-marker")

    from booley.runtime import runtime_context

    if not runtime_context.inside_session_runtime():
        _check_host_agent_session(_pass, _warn)
        _check_image_bakes_runtime_marker(
            docker_exe,
            sandbox_image,
            _pass,
            _warn,
            _skip,
        )
        return

    if os.environ.get("BOOLEY_CONTAINER") == "1":
        _pass("BOOLEY_CONTAINER=1 set (authoritative runtime detection)")
    else:
        _warn(
            "container detected only via the /.dockerenv fallback — "
            f"BOOLEY_CONTAINER=1 is missing; {_RUNTIME_MARKER_FIX}"
        )
    _check_slot_store_writable(_pass, _fail)


def _check_host_agent_session(_pass: Check, _warn: Check) -> None:
    """Name the one runtime-location mistake that is otherwise completely silent.

    MCP registration happens container-side (``booley.runtime.incontainer_register``,
    run from the devcontainer's postCreate/postStart hooks). An agent started
    from a *host* shell therefore has no ``booley`` MCP server at all: no
    ``booley_status``, no Booley Flows, no error either — the MCP tools are not
    there. Meanwhile the project's guidance file still tells it to call them,
    so the agent reads the absence as a transient outage and improvises raw
    EDA commands that bypass Booley's reports and ticket state entirely.
    """
    _warn = _warning_sink(_warn, "session.agent-on-host")

    from booley.runtime import runtime_context

    app = runtime_context.agent_session_app()
    if app is None:
        _pass("host shell — Booley Flows live in the Session Runtime (ADR 0028)")
        return
    _warn(
        f"{app} is running on the HOST: the Booley MCP server is registered only inside "
        "the Session Runtime, so booley_status and the Booley Flows (sim, lint, elab, "
        "synth) do not exist in this agent session",
        'reopen the project in the devcontainer ("Reopen in Container", or '
        "`booley session up && booley session enter`); for a one-off toolchain command "
        "on the host, use `booley shell -- <cmd>`",
    )


def _check_slot_store_writable(_pass: Check, _fail: Fail) -> None:
    """The shared admission store (ADR 0028 Decision 4) must accept claims."""
    from booley.runtime import job_slots

    root = job_slots.slots_dir()
    if root is None:
        _fail(
            "slot store root unresolvable (no project dir) — job admission cannot arbitrate",
            "run doctor from the project workspace or set BOOLEY_PROJECT_DIR",
        )
        return
    probe = root / f".doctor-write-probe-{os.getpid()}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        _fail(
            f"slot store root not writable: {root} ({exc})",
            f"fix ownership/permissions on {root}",
        )
        return
    _pass(f"slot store root writable: {root}")


def _check_image_bakes_runtime_marker(
    docker_exe: str | None,
    sandbox_image: str,
    _pass: Check,
    _warn: Check,
    _skip: Check,
) -> None:
    """Host-side runtime check: the image must bake the marker."""
    _warn = _warning_sink(_warn, "sandbox.runtime-marker", subject=sandbox_image)
    if not docker_exe:
        _skip("sandbox image runtime marker check skipped - container runtime unavailable")
        return
    try:
        result = subprocess.run(
            [docker_exe, "image", "inspect", sandbox_image, "--format", "{{json .Config.Env}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        _skip("sandbox image runtime marker check skipped - image inspect failed")
        return
    if result.returncode != 0:
        _skip(f"sandbox image runtime marker check skipped - image {sandbox_image} not present")
        return
    try:
        env_list = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        env_list = []
    if isinstance(env_list, list) and "BOOLEY_CONTAINER=1" in env_list:
        _pass(f"{sandbox_image} bakes BOOLEY_CONTAINER=1 (runtime marker)")
    else:
        _warn(f"{sandbox_image} does not bake BOOLEY_CONTAINER=1 — {_RUNTIME_MARKER_FIX}")


def _run_developer_probe(
    project: ProjectAudit,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """ADR 0028 Decision 12: measure the invariant's developer term.

    ``--deep``-only and in-container-only: the number that matters is the
    agent CLI's footprint *inside* the Session Runtime, where tickets actually
    run. Fail-soft for probe LIMITATIONS (no process accounting, no RSS
    reading, hit usage cap) — those degrade to a SKIP and the invariant keeps
    its 1 GiB fallback. But a failure of the agent CALL itself (auth, dead
    backend) is a real project defect — every ticket's developer would crash
    the same way at launch — and is reported as a FAIL, not hidden in a SKIP
    (the 2026-07-23 expired-creds incident sailed through a green doctor
    exactly this way). Doctor must never crash on the probe.
    """
    from booley.harness import developer_probe
    from booley.runtime import runtime_context

    if not runtime_context.inside_session_runtime():
        _skip(
            "developer memory probe: runs in-container "
            "(it measures the in-container agent footprint)"
        )
        return
    try:
        peak, exact = developer_probe.measure_developer_rss(
            project.project_root,
        )
        path = developer_probe.record_measurement(project.project_dir, peak)
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract (Decision 12); SKIP, never crash
        if getattr(exc, "agent_failure", False):
            _fail(
                f"developer probe agent could not complete a trivial call — every "
                f"ticket agent will fail the same way at launch: {exc}",
                "check agent auth at THIS runtime location (booley auth, or claude login + "
                "container recreate); see the harness log for the agent's error",
            )
            return
        _skip(f"developer memory probe skipped - {exc} (memory invariant keeps the 1g fallback)")
        return
    bound = "" if exact else " (upper bound)"
    _pass(
        f"developer peak RSS measured: {resource_policy.format_memory(peak)}{bound} — "
        f"recorded to {path}"
    )
    _pass("developer backend live authorization check completed successfully")


def _check_skills(_pass: Check, _warn: Check, _fail: Fail) -> None:
    """Check system-level skills directory."""
    _warn = _warning_sink(_warn, "host.skills-empty")
    home = Path.home()
    for skill_rel in _SKILL_DIRS:
        skills_dir = home / skill_rel
        if skills_dir.is_dir():
            skills_count = sum(1 for d in skills_dir.iterdir() if d.is_dir())
            if skills_count > 0:
                _pass(f"{skills_count} skill(s) in {skills_dir}")
            else:
                _warn(f"no skills in {skills_dir}; rerun booley init if skills are needed")
            return
    _fail(
        "no system-level skills directory found",
        "booley init",
    )


def _booley_dockerfile() -> Path | None:
    """Locate the Booley sandbox Dockerfile shipped with the package."""
    try:
        import booley
    except ImportError:
        return None
    path = Path(booley.__file__).resolve().parent / "data" / "docker" / "Dockerfile"
    return path if path.is_file() else None


def _parse_docker_created(value: str) -> float | None:
    """Parse a docker ``.Created`` RFC3339 timestamp into a UTC epoch."""
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?", value.strip())
    if not match:
        return None
    base = match.group(1)
    frac = (match.group(2) or "")[:7]
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if frac else "%Y-%m-%dT%H:%M:%S"
    try:
        dt = datetime.strptime(base + frac, fmt)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC).timestamp()


def _image_created_epoch(docker_exe: str, image: str) -> float | None:
    """Return an image's build time as a UTC epoch, or None if unavailable.

    ``None`` covers every "can't tell" case (missing image, docker error,
    unparseable timestamp) so callers degrade to silence instead of a false
    staleness verdict.
    """
    try:
        result = subprocess.run(
            [docker_exe, "image", "inspect", "--format", "{{.Created}}", image],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return _parse_docker_created(result.stdout)


def _check_image_freshness(
    docker_exe: str | None,
    image: str,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when the built sandbox image predates the Booley Dockerfile.

    ``booley init`` only builds the image when it is missing, so a Dockerfile
    edit (e.g. the agent-uid fix) is silently ignored until a forced rebuild.
    """
    _warn = _warning_sink(
        _warn,
        "sandbox.image-stale",
        subject=image,
        dedupe=image,
    )
    if not docker_exe or image != DOCKER_IMAGE:
        return
    dockerfile = _booley_dockerfile()
    if dockerfile is None:
        return
    created = _image_created_epoch(docker_exe, image)
    if created is None:
        return
    try:
        dockerfile_mtime = dockerfile.stat().st_mtime
    except OSError:
        return
    if dockerfile_mtime > created:
        _warn(
            f"sandbox image '{image}' predates {dockerfile.name}; booley init "
            "will not rebuild it - run booley init --force (or rebuild the image)"
        )
    else:
        _pass(f"sandbox image '{image}' is newer than {dockerfile.name}")


def _check_derived_image_freshness(
    project: ProjectAudit | None,
    docker_exe: str | None,
    image: str,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when a project's derived sandbox image predates its base image.

    The auto-generated project image (``<slug>-booley-sandbox``) is built
    ``FROM booley-sandbox`` and freezes the base layers present at *its* build
    time. Rebuilding the base alone (e.g. after a Booley upgrade that adds a
    new flow stage) does **not** propagate — the derived image keeps running the
    old baked-in Booley wheel until it too is rebuilt. That drift is invisible:
    the container silently executes stale code. Flag it so a base rebuild can't
    orphan project images unnoticed.

    Scoped to the *auto-generated* derived image only: a user-managed
    ``[sandbox].image`` (which need not derive from the base at all) is left
    alone, as is the base image itself (handled by ``_check_image_freshness``).
    """
    _warn = _warning_sink(
        _warn,
        "sandbox.image-stale",
        subject=image,
        dedupe=image,
    )
    if not docker_exe or project is None:
        return
    if image != pi.project_image_name(project.project_root):
        return  # user-managed or base image — not ours to judge
    base_created = _image_created_epoch(docker_exe, DOCKER_IMAGE)
    derived_created = _image_created_epoch(docker_exe, image)
    if base_created is None or derived_created is None:
        return  # base absent or unreadable — nothing to compare against
    if derived_created < base_created:
        _warn(
            f"project image '{image}' predates its base '{DOCKER_IMAGE}'; it "
            "carries a stale Booley build (e.g. missing a newer flow stage). "
            "Rebuild it with booley init --force."
        )
    else:
        _pass(f"project image '{image}' is newer than its base '{DOCKER_IMAGE}'")


def _check_custom_image_freshness(
    project: ProjectAudit | None,
    docker_exe: str | None,
    image: str,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when a hand-named ``[sandbox].image`` predates its project Dockerfile.

    ``_check_image_freshness`` guards the base image and
    ``_check_derived_image_freshness`` the auto-generated ``<slug>-booley-sandbox``.
    A *custom* ``[sandbox].image`` (e.g. ``openc910-booley-sandbox``, built from a
    hand-maintained ``.booley_project/docker/Dockerfile``) falls through both — so
    the classic "edited the Dockerfile, forgot to rebuild, container runs the old
    toolchain" drift goes unflagged. Compare the image build time against that
    Dockerfile's mtime; degrade to silence when there is nothing to compare.
    """
    _warn = _warning_sink(
        _warn,
        "sandbox.image-stale",
        subject=image,
        dedupe=image,
    )
    if not docker_exe or project is None:
        return
    if image == DOCKER_IMAGE or image == pi.project_image_name(project.project_root):
        return  # base / auto-generated derived — covered by the sibling checks
    dockerfile = project.project_dir / "docker" / "Dockerfile"
    if not dockerfile.is_file():
        return  # no project Dockerfile to compare against (e.g. externally built)
    created = _image_created_epoch(docker_exe, image)
    if created is None:
        return
    try:
        dockerfile_mtime = dockerfile.stat().st_mtime
    except OSError:
        return
    if dockerfile_mtime > created:
        _warn(
            f"custom sandbox image '{image}' predates {dockerfile}; the container "
            "runs the old toolchain until you rebuild it (edited Dockerfile, no "
            "rebuild)"
        )
    else:
        _pass(f"custom sandbox image '{image}' is newer than its Dockerfile")


def _booley_package_dir() -> Path | None:
    """The installed Booley package directory (for source-vs-image drift)."""
    try:
        import booley
    except ImportError:
        return None
    p = Path(booley.__file__).resolve().parent
    return p if p.is_dir() else None


def _newest_source_mtime(pkg_dir: Path) -> float | None:
    """Newest mtime among the package's ``*.py`` files, or None if none/unreadable."""
    newest: float | None = None
    for f in pkg_dir.rglob("*.py"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    return newest


def _check_image_bakes_current_booley(
    project: ProjectAudit | None,
    docker_exe: str | None,
    image: str,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when host Booley source is newer than the wheel baked into the image.

    One level deeper than :func:`_check_derived_image_freshness`: even an image
    that post-dates its base can bake a *Booley wheel* older than the current
    host source. The sandbox's egress lockdown forbids ``pip install`` at Flow
    time, so a host-side Booley fix stays invisible in-container until the image
    is rebuilt — the container silently runs stale code. ``pip show``'s static
    ``0.1.0`` (SETUP-1) can't reveal this. The exact answer is the
    ``booley.build-fingerprint`` image label — a content hash of the baked
    sources, stamped at build time and inherited by derived images — compared
    against the current checkout. When either side is unavailable (pip install
    with no checkout, an unlabeled or deliberately pulled image), fall back to
    the newest-host-source-mtime-vs-image-build-time heuristic the sibling
    probes use. Scoped to the base and auto-generated derived images; a
    user-managed ``[sandbox].image`` need not bake our wheel and is left alone.
    """
    _warn = _warning_sink(
        _warn,
        "sandbox.image-stale",
        subject=image,
        dedupe=image,
    )
    if not docker_exe:
        return
    generated = pi.project_image_name(project.project_root) if project else None
    if image not in (DOCKER_IMAGE, generated):
        return
    mismatch = source_fingerprint_mismatch(image)
    if mismatch is not None:
        if mismatch:
            _warn(
                f"'{image}' bakes Booley sources that no longer match this "
                "checkout (build-fingerprint label differs); the sandbox runs "
                "stale code (egress is locked, so it can't pip-install the "
                "fix). Rebuild with booley init --force."
            )
        else:
            _pass(f"'{image}' bakes exactly this checkout's Booley sources")
        return
    pkg = _booley_package_dir()
    created = _image_created_epoch(docker_exe, image)
    if pkg is None or created is None:
        return
    newest = _newest_source_mtime(pkg)
    if newest is None:
        return
    if newest > created:
        _warn(
            f"host Booley source is newer than the build baked into '{image}'; "
            "the sandbox runs stale code (egress is locked, so it can't pip-"
            "install the fix). Rebuild with booley init --force."
        )
    else:
        _pass(f"'{image}' bakes Booley no older than the host source")


def _check_container_uid(  # noqa: PLR0911 — ordered precondition ladder; each early return is a distinct skip/failure case
    project: ProjectAudit | None,
    docker_exe: str | None,
    image: str,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
) -> None:
    """Compare the in-container agent uid against the .booley_project owner.

    On native Linux, bind-mounted project files keep their host uid; if the
    container user has a different uid it cannot write into .booley_project and
    the MCP server fails opaquely. Docker Desktop (macOS/Windows) remaps mount
    ownership, so this check only applies on Linux hosts.
    """
    _warn = _warning_sink(_warn, "sandbox.uid-probe", subject=image)
    if project is None or not docker_exe:
        return
    if not sys.platform.startswith("linux") or not hasattr(os, "getuid"):
        return
    try:
        result = subprocess.run(
            [docker_exe, "run", "--rm", image, "id", "-u"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        _warn(f"could not probe container uid: {exc}")
        return
    if result.returncode != 0:
        _warn("could not probe container uid")
        return
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    try:
        container_uid = int(lines[-1])
    except (IndexError, ValueError):
        _warn(f"unexpected container uid output: {result.stdout.strip()!r}")
        return
    try:
        owner_uid = project.project_dir.stat().st_uid
    except OSError as exc:
        _warn(f"could not stat {project.project_dir}: {exc}")
        return
    if container_uid == owner_uid:
        _pass(f"container agent uid {container_uid} matches .booley_project owner")
        return
    _fail(
        f"container agent uid {container_uid} != .booley_project owner uid "
        f"{owner_uid}; bind-mounted files are unwritable in the sandbox",
        f"rebuild the sandbox image with the agent user pinned to host uid {os.getuid()}",
    )


def _project_declares_verible_lint(project: ProjectAudit | None) -> bool:
    """True when some ``.core`` lint Target selects the Verible EDA tool (ADR 0033).

    A cheap ``.core`` YAML read (no subprocess) — the same enumeration the
    Booley Flows' ``--target`` validation drives off. Gates the container check for
    ``verible-verilog-lint`` so projects without a Verible Target are never
    nagged about a binary they don't use.
    """
    if project is None:
        return False
    try:
        refs = fusesoc_registry.enumerate_targets(project.project_root)
    except Exception:  # noqa: BLE001 — unreadable .core files are their own doctor findings
        return False
    return any(
        ref.flow == "lint" and "verible" in (ref.eda_tool or "").lower() for ref in refs.values()
    )


def _no_docker_skip_reason() -> str:
    """Why the container checks are being skipped, in the reader's own terms.

    Reading "runtime or sandbox image not available" from *inside* the Session
    Runtime sounds like a fault (fpu F-19). It isn't: the sandbox deliberately
    has no nested Docker, and these checks probe the *host's* images — so say
    that, instead of a message written for the host case.
    """
    from booley.runtime import runtime_context

    if runtime_context.inside_session_runtime():
        return (
            "container checks not applicable in here - you are already inside the "
            "Session Runtime, which has no nested Docker. Run `booley doctor` on "
            "the host to audit the sandbox images"
        )
    return "container checks skipped - no Docker/Podman runtime found on this host"


def _check_current_runtime_web_isolation(_pass: Check, _fail: Fail) -> None:
    """Validate provider-side web policy from inside the Session Runtime."""
    from booley.harness.web_isolation import policy_error

    error = policy_error()
    if error:
        _fail(
            f"agent provider-side web access is not disabled: {error}",
            "rebuild the sandbox image, then rebuild/reopen the Session Runtime",
        )
        return
    _pass("agent provider-side web access disabled")


def _check_container_runtime_payload(
    check: Callable[[str, list[str], str], None],
) -> None:
    """Check Booley, bwave, and provider-web policy baked into the image."""
    check(
        "bwave binary (native)",
        [
            "python3",
            "-c",
            "from booley.runtime.paths import native_bwave_binary as n; raise SystemExit(0 if n() else 1)",
        ],
        "rebuild sandbox image",
    )
    check(
        "bwave CLI on PATH is the wrapper (`bwave gui`)",
        ["bwave", "gui", "--help"],
        "rebuild the sandbox image — its ~/.cargo/bin/bwave shadows the wrapper on PATH",
    )
    check(
        "booley package in container", ["python3", "-c", "import booley"], "rebuild sandbox image"
    )
    check(
        "agent provider-side web access disabled",
        ["python3", "-m", "booley.harness.web_isolation"],
        "rebuild the sandbox image, then rebuild/reopen the Session Runtime",
    )


def _run_container_checks(
    project: ProjectAudit | None,
    docker_exe: str | None,
    image: str,
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Run container EDA tool and runtime checks."""
    if not docker_exe:
        banner("Container checks")
    from booley.runtime import runtime_context

    if runtime_context.inside_session_runtime():
        _check_current_runtime_web_isolation(_pass, _fail)
        _skip(_no_docker_skip_reason())
        return

    banner("Container checks")
    if _docker_image_exists_by_name(image):
        _pass(f"{image} image present")
    else:
        _fail(f"{image} image missing", "booley init or build the configured sandbox image")
        return

    _check_image_freshness(docker_exe, image, _pass, _warn)
    _check_derived_image_freshness(project, docker_exe, image, _pass, _warn)
    _check_custom_image_freshness(project, docker_exe, image, _pass, _warn)
    _check_image_bakes_current_booley(project, docker_exe, image, _pass, _warn)
    _check_container_uid(project, docker_exe, image, _pass, _warn, _fail)

    def _container_check(description: str, cmd: list[str], fix: str = "") -> None:
        try:
            result = subprocess.run(
                [docker_exe, "run", "--rm", image, *cmd],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                _pass(description)
                if verbose and result.stdout.strip():
                    info(f"    {result.stdout.strip().splitlines()[0]}")
            else:
                _fail(description, fix)
        except (subprocess.SubprocessError, FileNotFoundError):
            _fail(f"{description} (timeout/error)", fix)

    _container_check("verilator", ["verilator", "--version"], "rebuild sandbox image")
    _container_check("yosys", ["yosys", "-V"], "rebuild sandbox image")
    _container_check("iverilog", ["iverilog", "-V"], "rebuild sandbox image")
    _container_check("sv2v", ["sv2v", "--version"], "rebuild sandbox image")
    # Conditional (ADR 0033 decision 8): only a project whose .core declares a
    # Verible lint Target needs the binary — an unconditional check would nag
    # every project that doesn't use it, so absent that Target there is no
    # check and no output at all.
    if _project_declares_verible_lint(project):
        _container_check(
            "verible-verilog-lint (a .core lint Target selects verible)",
            ["verible-verilog-lint", "--version"],
            "rebuild the sandbox image — it predates Verible support (ADR 0033)",
        )
    _container_check("ripgrep", ["rg", "--version"], "rebuild sandbox image")
    # Two distinct things, and an image can have one without the other:
    #   - the native binary, off PATH, which every query/FIFO caller runs;
    #   - the Python wrapper, the only `bwave` on PATH, which owns the `gui`
    #     verb. Images built before the binary moved out of ~/.cargo/bin have
    #     it shadowing the wrapper on PATH, so `bwave gui` fails for a human
    #     at a shell while the MCP tool (which calls the wrapper directly)
    #     keeps working — exactly the asymmetry this check makes visible.
    _check_container_runtime_payload(_container_check)

    # Conditional (ADR 0034 / F1): only a project with a Cocotb Target needs
    # cocotb in the image — the vanilla SV project is never faulted for it.
    if _project_has_cocotb_target(project):
        _container_check(
            "cocotb in container (a Cocotb Target exists)",
            ["cocotb-config", "--version"],
            "sandbox image predates cocotb support — rebuild the sandbox image",
        )

    _check_riscv_toolchain(docker_exe, image, _pass, _skip, _fail)


# RISC-V variant checks fire only when the image bakes this flavour marker
# (Dockerfile.riscv), so the vanilla base sandbox is never faulted for lacking a
# cross-toolchain it was never meant to carry.
_RISCV_IMAGE_FIX = eda_environment.RISCV_IMAGE_FIX
_RISCV_DOC_FILES = eda_environment.RISCV_DOC_FILES


def _check_riscv_toolchain(
    docker_exe: str,
    image: str,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Render the extracted RISC-V image toolchain audit."""
    flavor = _image_env_value(docker_exe, image, "BOOLEY_SANDBOX_FLAVOR")
    findings = eda_environment.audit_riscv_toolchain(
        docker_exe,
        image,
        flavor,
        run=subprocess.run,
    )
    for finding in findings:
        if finding.severity is eda_environment.EdaFindingSeverity.PASS:
            _pass(finding.message)
        else:
            _fail(finding.message, finding.fix)


def _run_mcp_checks(
    project: ProjectAudit | None,
    docker_exe: str | None,
    image: str,
    verbose: bool,
    reporter: _Reporter,
) -> None:
    """Validate Interactive Mode: devcontainer spec, excludes, Docker objects,
    auth token, and in-container MCP server discovery (ADR 0018)."""
    banner("Interactive Mode checks")
    if project is None:
        reporter.skip_("Interactive Mode checks skipped - project config invalid")
        return

    _check_devcontainer_spec(
        project.project_root,
        image,
        _declared_provider(project),
        reporter.pass_,
        reporter.warn_,
        reporter.fail_,
        _note=reporter.note_,
    )
    _check_issued_session_runtime(
        project, docker_exe, reporter.pass_, reporter.skip_, reporter.fail_
    )
    _check_devcontainer_excludes(project.project_root, reporter.pass_, reporter.warn_)
    _check_interactive_logs_gitignore(project.project_dir, reporter.pass_, reporter.warn_)
    _check_interactive_logs_tracked(project.project_dir, reporter.pass_, reporter.fail_)
    _run_agent_credential_checks(project, reporter)
    _check_interactive_docker_objects(docker_exe, reporter.pass_, reporter.warn_, reporter.skip_)
    _check_wcp_server(project, docker_exe, reporter.pass_, reporter.skip_, reporter.fail_)
    _check_interactive_state_volumes(
        project, docker_exe, verbose, reporter.pass_, reporter.note_, reporter.skip_
    )
    _check_issued_image_keepers(
        project, docker_exe, verbose, reporter.pass_, reporter.note_, reporter.skip_
    )

    if not docker_exe:
        reporter.skip_("MCP server probe skipped - container runtime unavailable")
        return
    if not _docker_image_exists_by_name(image):
        reporter.skip_("MCP server probe skipped - sandbox image unavailable")
        return
    _run_mcp_probe(
        project, docker_exe, image, verbose, reporter.pass_, reporter.warn_, reporter.fail_
    )


def _run_agent_credential_checks(project: ProjectAudit, reporter: _Reporter) -> None:
    """Run credential checks when the invocation profile includes agents."""
    if not reporter.agent_check_enabled("agent credential checks"):
        return
    provider = _configured_provider(project)
    auth_policy = _configured_auth_policy(project)
    _check_agent_auth_token(
        provider, reporter.pass_, reporter.warn_, reporter.skip_, policy=auth_policy
    )
    _check_oauth_token(
        provider,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        policy=auth_policy,
        _note=reporter.note_,
    )
    _check_subscription_creds_health(provider, reporter.pass_, reporter.warn_, policy=auth_policy)


def _check_interactive_logs_gitignore(
    project_dir: Path,
    _pass: Check,
    _warn: Check,
) -> None:
    _warn = _warning_sink(_warn, "interactive.logs-gitignore")
    gitignore = project_dir / ".gitignore"
    if not gitignore.is_file():
        _warn(".booley_project/.gitignore missing; interactive logs may be tracked")
        return
    try:
        content = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _warn(f"could not read {gitignore}: {exc}")
        return
    if ".interactive_logs/" in content:
        _pass("interactive MCP logs are gitignored")
    else:
        _warn(".booley_project/.gitignore should include .interactive_logs/")


def _check_interactive_logs_tracked(
    project_dir: Path,
    _pass: Check,
    _fail: Fail,
) -> None:
    """Fail when ephemeral .interactive_logs/ scratch is committed to git.

    Tracked scratch is laid down read-protected on fresh clones and makes the
    in-container MCP server fail with an opaque PermissionError. The gitignore
    check alone does not catch an already-poisoned repository.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", ".interactive_logs"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    if tracked:
        _fail(
            f".interactive_logs/ has {len(tracked)} tracked file(s); committed "
            "scratch breaks fresh clones in the sandbox",
            "git rm -r --cached .interactive_logs && commit the removal",
        )
    else:
        _pass(".interactive_logs/ is not tracked in git")


def _devcontainer_tracked(project_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", ".devcontainer"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _check_devcontainer_spec(  # noqa: PLR0911,PLR0912 — ordered drift precondition ladder
    project_root: Path,
    sandbox_image: str,
    declared_provider: str | None,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
) -> None:
    """ADR 0018: untracked, valid devcontainer.json; never a tracked one.

    *sandbox_image* is the project-resolved ``[sandbox].image``; the spec's own
    ``image`` must match it, else the Session Runtime runs a stale image.
    *declared_provider* is the project's *explicit* ``[agent] provider``
    (``None`` = undeclared); the spec's ``BOOLEY_AGENT_APP`` must match it.
    """
    note_sink = _note or _pass
    _warn = _warning_sink(
        _warn,
        "interactive.devcontainer-drift",
        subject=str(devcontainer_path(project_root)),
    )
    if _devcontainer_tracked(project_root):
        _fail(
            ".devcontainer/ is tracked by git - Interactive Mode unavailable",
            "remove it from git history; Booley keeps the spec untracked",
        )
        return
    path = devcontainer_path(project_root)
    if not path.is_file():
        _warn("no .devcontainer/devcontainer.json - run `booley init` (or `--seed`)")
        return
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"{path} does not parse: {exc}", "re-run booley init")
        return
    # Agent-app drift: the spec's app is chosen from [agent] provider at seed
    # time, so a project that adopts (or switches) a provider afterwards leaves
    # the untracked spec pointing at the previous app. Everything downstream is
    # keyed off it, and every consequence is SILENT:
    #   - incontainer_register writes the Booley MCP entry into the *other*
    #     app's config, so the running agent sees no Booley MCP tools at all while
    #     the HTTP server sits there healthy — the failure looks like a dead
    #     server from every angle except the agent's own `mcp list`.
    #   - the state volume targets the other app's home-state dir, so the real
    #     agent's sessions/memories live in the writable layer and die on the
    #     next rebuild.
    #   - the credential seed mounts the other app's auth files.
    # Checked BEFORE the persistence check below on purpose: a mismatched spec
    # still mounts a volume for the app it names, so that check would report a
    # cheerful pass while the agent actually in use persists nothing.
    spec_app = dc.spec_agent_app(spec)
    if declared_provider is not None and spec_app is not None and spec_app != declared_provider:
        _fail(
            f"devcontainer.json BOOLEY_AGENT_APP '{spec_app}' != [agent] provider "
            f"'{declared_provider}': the Booley MCP entry is registered for "
            f"'{spec_app}', so a '{declared_provider}' session sees no Booley "
            f"MCP tools, and '{declared_provider}' home-state is not persisted",
            "re-run `booley init --seed`, then rebuild the container in VS Code",
        )
        return
    # A spec predating the home-state persistence fix mounts no volume at the
    # agent's ~/.claude (etc.), so in-container transcripts/plans/todos vanish on
    # every rebuild. Regenerating is untracked and cheap, so warn (not fail).
    if spec_state_is_persisted(spec) is False:
        _warn(
            "devcontainer.json is stale: no persistent volume for the agent's "
            "home-state - in-container transcripts/plans are lost on every "
            "rebuild; re-run `booley init` to regenerate"
        )
        return
    # Image drift: the spec is generated from [sandbox].image, but a project
    # that sets/changes that image *after* the first `booley init` (e.g. adds a
    # custom project image with an extra toolchain) leaves the untracked spec
    # frozen on the old image. The Session Runtime then runs the wrong image
    # and the Flows silently miss their EDA toolchains, indistinguishable
    # from a real failure. Regenerating is cheap and untracked, so warn.
    spec_image = spec.get("image")
    resolved_image = idk.image_id(sandbox_image)
    immutable_spec = isinstance(spec_image, str) and re.fullmatch(
        r"sha256:[0-9a-f]{64}", spec_image
    )
    if immutable_spec and resolved_image is None:
        note_sink(
            "devcontainer.json has an immutable image pin, but [sandbox].image "
            f"'{sandbox_image}' cannot be resolved here; image drift is unverified "
            "and host `booley doctor` is authoritative"
        )
    elif isinstance(spec_image, str) and spec_image != (resolved_image or sandbox_image):
        _warn(
            f"devcontainer.json image '{spec_image}' != immutable ID for [sandbox].image "
            f"'{sandbox_image}': the Session Runtime runs a stale image "
            "(missing toolchains it should have) - re-run `booley init --seed`, "
            "then rebuild the container in VS Code"
        )
        return
    # The setup-managed Nangate cache lives on the host, not in the image.
    # A session-spec refresh once dropped this mount while leaving the verified
    # cache intact, so synthesis failed only after a ticket reached its QoR
    # gate. Host Doctor can see the cache and must catch that drift up front.
    if nangate_pdk.is_ready() and not dc.spec_mounts_target(spec, nangate_pdk.CONTAINER_ROOT):
        _warn(
            "devcontainer.json omits the verified Nangate45 cache mount at "
            f"{nangate_pdk.CONTAINER_ROOT}: ASIC synthesis will fail before "
            "elaboration - re-run `booley init --seed`, then rebuild the container"
        )
        return
    # Token-seed drift: a credential stored by `booley auth` reaches VS Code
    # sessions only through the spec's sidecar mount (VS Code resolves the
    # ${localEnv:...} route against its own env, where the store is invisible).
    # A spec seeded before the credential was stored has no mount, so those
    # sessions silently run on the refreshing credential instead.
    remote_env = spec.get("remoteEnv")
    app = remote_env.get("BOOLEY_AGENT_APP") if isinstance(remote_env, dict) else None
    if (
        app in auth_token.CREDENTIALS
        and auth_token.read_stored_token(app)
        and spec_mounts_token_seed(spec) is False
    ):
        _warn(
            "devcontainer.json predates the stored `booley auth` credential: "
            "VS Code sessions fall back to the refreshing one - re-run "
            "`booley init --seed`, then rebuild the container in VS Code"
        )
        return
    # Waveform Viewer drift (ADR 0035): VaporView and its WCP settings reach
    # the container only via the spec's customizations - never via the image -
    # so a spec seeded before the viewer landed leaves every scoped
    # `bwave gui` failing with "WCP server not running", rebuild or not.
    if not dc.spec_installs_vaporview(spec):
        _warn(
            "devcontainer.json predates the Waveform Viewer (ADR 0035) or its "
            "WCP auto-start patch: VaporView/WCP settings or the postAttach "
            "manifest patch are missing, so in-container `bwave gui` finds no "
            "running viewer - re-run `booley init --seed`, then rebuild the "
            "container in VS Code"
        )
        return
    # Highlighting drift: same delivery path as VaporView (spec-only, never
    # the image), so a spec seeded before the grammar extensions landed
    # renders RTL and SDC/XDC constraints as plain text in attached windows.
    # Cosmetic, but a re-seed is cheap.
    if not dc.spec_installs_hdl_highlight(spec):
        note_sink(
            "devcontainer.json predates the Verilog/SystemVerilog + Tcl "
            "highlighting extensions: RTL and SDC/XDC constraints render as "
            "plain text in attached VS Code windows - re-run "
            "`booley init --seed`, then reload the container window"
        )
        return
    # Rendered-report drift: Live Preview is also spec-delivered. Without it,
    # self-contained review HTML can be opened only as source inside a
    # container that deliberately has no desktop browser. A fixed port can
    # likewise inherit a dead tunnel owned by VS Code's long-lived local
    # process. This breaks a supported workflow, so it is a warning rather than
    # a cosmetic note: Doctor must not call this runtime green.
    if not dc.spec_installs_live_preview(spec):
        _warn(
            "devcontainer.json lacks the collision-safe Live Preview setup: "
            "rendered review HTML may open as a blank preview - re-run init "
            "with `--seed`, then rebuild the container in VS Code"
        )
        return
    # A synced Python extension discovers host-created workspace virtualenvs
    # asynchronously and injects their activation command into new terminals.
    # The runtime already provides its Python stack, so this is both unnecessary
    # and disruptive when the command lands while a CLI/TUI owns the terminal.
    if not dc.spec_disables_python_terminal_activation(spec):
        note_sink(
            "devcontainer.json predates the Python terminal activation fix: a "
            "synced Python extension can inject a delayed `source .venv/bin/activate` "
            "into new terminals - re-run init with `--seed`, then rebuild the "
            "container in VS Code"
        )
        return
    _pass("devcontainer.json present and valid")


def _check_issued_session_runtime(  # noqa: PLR0911 - ordered fail-closed audit gates
    project: ProjectAudit,
    docker_exe: str | None,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Enforce the immutable host issuance and mounted-Vivado runtime contract."""
    from booley.eda.config import PROVISIONING_HOST, EdaConfigError, parse_eda_config

    try:
        eda = parse_eda_config(project.booley_toml.get("eda"))
    except EdaConfigError as exc:
        _fail(f"Session Runtime EDA configuration is invalid: {exc}", "fix booley.toml [eda]")
        return

    vivado = eda.get("vivado")
    if runtime_context.inside_session_runtime():
        if not _check_runtime_isolation(_pass, _fail):
            return
        mounted = Path("/opt/booley-eda/vivado").is_dir()
        if (
            vivado is not None
            and vivado.provisioning == PROVISIONING_HOST
            and _flow_selection(project, "fpga").enabled
            and not mounted
        ):
            _fail(
                "host-provisioned Vivado is absent from the Session Runtime",
                "reissue the spec on the host and recreate the Session Runtime",
            )
            return
        if mounted:
            _check_mounted_vivado_runtime(_pass, _fail)
        else:
            _pass("Session Runtime has no active host-mounted commercial EDA request")
        return

    from booley.eda import runtime_spec

    path = devcontainer_path(project.project_root)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        issuance = runtime_spec.validate(project.project_root, spec, path)
    except (OSError, json.JSONDecodeError, runtime_spec.RuntimeSpecError) as exc:
        _fail(
            f"Session Runtime host issuance is invalid: {exc}",
            "run `booley init --seed` on the host and recreate the Session Runtime",
        )
        return
    _pass(f"Session Runtime spec has valid host issuance ({issuance.spec_sha256[:12]})")

    if not docker_exe:
        _skip("live issued Session Runtime labels/topology - container runtime unavailable")
        return
    identity = next(
        label for label in runtime_spec.labels(issuance) if label.startswith("booley.project-id=")
    )
    try:
        result = subprocess.run(
            [
                docker_exe,
                "ps",
                "-aq",
                "--filter",
                f"label={identity}",
                "--filter",
                f"label={dc.INTERACTIVE_ROLE_LABEL}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail(f"could not inspect issued Session Runtime resources: {exc}", "start Docker")
        return
    containers = [name for name in result.stdout.splitlines() if name]
    drifted = [
        name
        for name in containers
        if not session_runtime._container_matches_issuance(
            name,
            issuance,
            spec=spec,
            workspace=project.project_root,
        )
    ]
    if result.returncode != 0 or drifted:
        _fail(
            "live Session Runtime state differs from current host issuance",
            "run `booley session down`, then `booley session up --rebuild`",
        )
    elif containers:
        _pass("live Session Runtime state matches the current host issuance")
    else:
        _pass("no stale live Session Runtime resources for this Project")
    _check_issued_license_relay(
        project.project_root,
        containers,
        issuance,
        _pass,
        _fail,
    )


def _check_runtime_isolation(_pass: Check, _fail: Fail) -> bool:
    """Enforce authority absence and fixed Project-data identity in every runtime."""
    if os.environ.get("BOOLEY_PROJECT_DIR") != "/booley-project":
        _fail(
            "Session Runtime Project-data identity differs from /booley-project",
            "reissue the spec on the host and recreate the Session Runtime",
        )
        return False
    required = Path("/booley-project")
    forbidden = (
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path("/root/.ssh"),
        Path("/home/agent/.ssh"),
        Path("/root/.config/booley/eda"),
        Path("/home/agent/.config/booley/eda"),
    )
    if not required.is_dir() or any(_runtime_path_exposed(path) for path in forbidden):
        _fail(
            "Session Runtime exposes a forbidden host-authority surface",
            "reissue the spec on the host and recreate the Session Runtime",
        )
        return False
    _pass("Session Runtime Project data and host-authority isolation verified")
    return True


def _runtime_path_exposed(path: Path) -> bool:
    """Treat an agent-inaccessible root path as isolated, not as a Doctor crash."""
    try:
        return path.exists()
    except PermissionError:
        return False


def _check_issued_license_relay(
    project_root: Path,
    containers: list[str],
    issuance: object,
    _pass: Check,
    _fail: Fail,
) -> None:
    """Validate exact live relay bytes, endpoints, aliases, and hardening."""
    from booley.eda import runtime_spec
    from booley.eda.flexnet_docker import (
        RelayDockerError,
        RelayProfile,
        resources_for_session,
        validate_relay,
    )

    try:
        profile = runtime_spec.requested_license(project_root)
    except runtime_spec.RuntimeSpecError as exc:
        _fail(f"License Profile authority is invalid: {exc}", "repair the host EDA authority")
        return
    if profile is None:
        return
    image_id = getattr(issuance, "relay_image_id", None)
    if not isinstance(image_id, str):
        _fail("issued License Profile lacks an immutable relay image", "run `booley init --seed`")
        return
    relay = resources_for_session(str(project_root.resolve()))
    if not session_runtime._relay_objects_exist(relay):
        if containers:
            _fail(
                "licensed Session Runtime has no relay topology",
                "run `booley session up --rebuild`",
            )
        return
    try:
        validate_relay(
            relay,
            containers[0] if len(containers) == 1 else None,
            RelayProfile(
                profile.server_ipv4,
                profile.server_hostid,
                profile.lmgrd_port,
                profile.vendor_port,
            ),
            issuance_labels=runtime_spec.labels(issuance),
            image=image_id,
        )
    except RelayDockerError as exc:
        _fail(
            f"live FlexNet relay topology differs from host issuance: {exc}",
            "run `booley session down`, then `booley session up --rebuild`",
        )
        return
    _pass("live FlexNet relay bytes, hardening, endpoints, and aliases verified")


def _check_mounted_vivado_runtime(  # noqa: PLR0911 - ordered fail-closed runtime gates
    _pass: Check, _fail: Fail
) -> None:
    """Prove wrapper, release mount, architecture support, and runtime identity."""
    from booley.eda.vivado import CONTAINER_TARGET, SUPPORTED_VERSION, wrapper_sha256

    wrapper = Path("/usr/local/bin/vivado")
    executable = Path(CONTAINER_TARGET) / "Vivado" / "bin" / "vivado"
    try:
        digest = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    except OSError as exc:
        _fail(f"mounted Vivado wrapper is unreadable: {exc}", "rebuild the Session Runtime image")
        return
    if digest != wrapper_sha256() or not os.access(executable, os.X_OK):
        _fail(
            "mounted Vivado wrapper/release layout differs from built-in policy",
            "rebuild the image and reissue the Session Runtime spec on the host",
        )
        return
    compatibility = (
        Path("/usr/lib/x86_64-linux-gnu/libudev.so.1"),
        Path("/usr/lib/x86_64-linux-gnu/libpixman-1.so.0"),
        Path("/usr/lib/locale/locale-archive"),
    )
    if any(not path.is_file() for path in compatibility):
        _fail(
            "Session Runtime image lacks the fixed Vivado compatibility libraries or locale",
            "rebuild the Session Runtime image and reissue the spec",
        )
        return
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot inspect mounted Vivado release: {exc}", "recreate the Session Runtime")
        return
    fields = [line.split(" - ", 1)[0].split() for line in mountinfo.splitlines()]
    matches = [parts for parts in fields if len(parts) > 5 and parts[4] == CONTAINER_TARGET]
    if len(matches) != 1 or "ro" not in matches[0][5].split(","):
        _fail(
            "Vivado release root is not one exact read-only runtime mount",
            "reissue the spec and recreate the Session Runtime",
        )
        return
    license_pointer = os.environ.get("XILINXD_LICENSE_FILE")
    if (
        license_pointer is not None
        and re.fullmatch(r"[1-9][0-9]{0,4}@booley-license-xilinx", license_pointer) is None
    ):
        _fail(
            "XILINXD_LICENSE_FILE differs from the fixed private-relay contract",
            "reissue the Session Runtime from the host License Profile",
        )
        return
    try:
        result = subprocess.run(
            [str(wrapper), "-version"],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail(f"Vivado runtime identity probe failed: {exc}", "check the mounted installation")
        return
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or f"vivado v{SUPPORTED_VERSION}" not in output.lower():
        _fail(
            f"mounted Vivado did not report exact version {SUPPORTED_VERSION}",
            "register and grant an exact supported Vivado installation",
        )
        return
    _pass(f"mounted Vivado {SUPPORTED_VERSION} wrapper, read-only release, and identity verified")


def _check_devcontainer_excludes(
    project_root: Path,
    _pass: Check,
    _warn: Check,
) -> None:
    """ADR 0018: Booley files hidden via the honored info/exclude (not .gitignore)."""
    common = _git_common_dir(project_root)
    exclude = (common / "info" / "exclude") if common else None
    body = exclude.read_text(encoding="utf-8") if exclude and exclude.is_file() else ""
    missing = [n for n in (".devcontainer", ".booley_project") if f"/{n}" not in body]
    if missing:
        for entry in missing:
            _warning_sink(
                _warn,
                "project.git-excludes-missing",
                subject=entry,
            )(f"git info/exclude missing Booley entry: {entry}")
    else:
        _pass("Booley files excluded from git (info/exclude)")


def _declared_provider(project: ProjectAudit | None) -> str | None:
    """The project's *explicitly declared* ``[agent] provider``, else ``None``.

    Deliberately stricter than :func:`_configured_provider`, and deliberately
    NOT env-aware: this exists to drift-check the on-disk devcontainer spec, so
    it must mirror exactly what re-seeding would decide --
    ``init_cmd._select_interactive_app`` consults booley.toml and nothing else.
    An undeclared provider means the seeder falls back to host detection, which
    no on-disk value can be compared against, so the caller skips the check
    rather than guessing.

    Fail-soft (Decision 12): an invalid value degrades to ``None`` here;
    :func:`_validate_agent_table` already FAILed on it upstream.
    """
    from booley.config.agent import BackendConfigError, _parse_provider

    agent_section = project.booley_toml.get("agent") if project is not None else None
    if not isinstance(agent_section, dict):
        return None
    try:
        return _parse_provider(agent_section)
    except BackendConfigError:
        return None


def _configured_provider(project: ProjectAudit | None) -> str:
    """Return the single agent provider this project's runs will use.

    Booley runs exactly one provider, and there is ALWAYS an answer: this
    mirrors ``_backend_config._lazy_backend_config`` --
    ``BOOLEY_PRIMARY_PROVIDER`` -> booley.toml ``[agent] provider`` ->
    ``BOOLEY_AGENT_APP`` (exported by the devcontainer) -> ``_DEFAULT_PROVIDER``.
    An omitted ``[agent] provider`` is not "unknown", it is the default, so the
    auth checks audit exactly the backend that will run: the *unused* backend's
    absent credentials -- guaranteed on any single-provider host, with no action
    worth taking -- never surface as warnings.

    Doctor is fail-soft by contract (Decision 12), so an invalid provider value
    degrades to the default here instead of raising --
    :func:`_validate_agent_table` already FAILed on it upstream.
    """
    from booley.config.agent import (
        _DEFAULT_PROVIDER,
        _VALID_PROVIDERS,
        BackendConfigError,
        _parse_provider,
    )

    env_provider = (os.environ.get("BOOLEY_PRIMARY_PROVIDER") or "").strip()
    if env_provider in _VALID_PROVIDERS:
        return env_provider
    agent_section = project.booley_toml.get("agent") if project is not None else None
    if isinstance(agent_section, dict):
        try:
            declared = _parse_provider(agent_section)
        except BackendConfigError:
            declared = None
        if declared is not None:
            return declared
    app_provider = (os.environ.get("BOOLEY_AGENT_APP") or "").strip()
    if app_provider in _VALID_PROVIDERS:
        return app_provider
    return _DEFAULT_PROVIDER


def _configured_auth_policy(project: ProjectAudit | None) -> str:
    """The project's ``[agent] auth`` policy, defaulting to ``auto``.

    Mirrors :func:`_configured_provider`: ``BOOLEY_PRIMARY_AUTH`` beats the
    project's booley.toml. Doctor is fail-soft, so an invalid toml value
    degrades to ``auto`` here — :func:`_validate_agent_table` already FAILed
    on it upstream.
    """
    from booley.config.agent import (
        _DEFAULT_AUTH,
        _VALID_AUTH_MODES,
        BackendConfigError,
        _parse_auth,
    )

    env_auth = (os.environ.get("BOOLEY_PRIMARY_AUTH") or "").strip()
    if env_auth in _VALID_AUTH_MODES:
        return env_auth
    if project is None:
        return _DEFAULT_AUTH
    agent_section = project.booley_toml.get("agent")
    if not isinstance(agent_section, dict):
        return _DEFAULT_AUTH
    try:
        return _parse_auth(agent_section) or _DEFAULT_AUTH
    except BackendConfigError:
        return _DEFAULT_AUTH


_AGENT_APP_LABELS = {auth_token.APP_CLAUDE: "Claude Code", auth_token.APP_CODEX: "Codex"}


def _agent_app_installed(provider: str) -> bool:
    """Whether *provider*'s CLI is on this host (indirection keeps both
    detectors monkeypatchable as module globals)."""
    if provider == auth_token.APP_CLAUDE:
        return _detect_claude_code()
    return _detect_codex()


def _provider_not_installed_reason(provider: str) -> str:
    """Explain an unchecked auth audit: the selected provider is absent here."""
    return f"the configured [agent] provider ({provider}) is not installed on this host"


def _check_agent_auth_token(
    provider: str,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    policy: str = "auto",
) -> None:
    """ADR 0018: verify the agent's credential where it actually lives (not assumed).

    Reports the credential that WINS under the agent CLI's own precedence (see
    :func:`auth_token.effective_credential`), naming anything it outranks — a
    subscription login outranked by an exported API key used to be reported as
    the active auth, which misstated what bills. *policy* is the project's
    ``[agent] auth`` knob: under ``subscription`` Booley scrubs the API keys
    from agent envs, so the resolution here mirrors that.

    Scoped to the ONE provider the project runs on (see
    :func:`_configured_provider`): a Booley run never reads the other app's
    credentials, so their absence is not a finding worth a warning.
    """
    _warn = _warning_sink(_warn, "agent.credential-missing", subject=provider)
    if not _agent_app_installed(provider):
        _skip(f"agent auth token check skipped - {_provider_not_installed_reason(provider)}")
        return
    label = _AGENT_APP_LABELS[provider]
    effective = auth_token.effective_credential(provider, policy=policy)
    if effective is None:
        hint = (
            f"[agent] auth = '{policy}' pins the billing mode — provide that credential "
            "or change the knob"
            if policy != "auto"
            else "run its login or export an API key"
        )
        _warn(
            f"{label} detected but no usable credential under auth = '{policy}' "
            f"(login file: {auth_token.subscription_creds_path(provider)}) — {hint}"
        )
        return
    line = f"{label} auth: {effective.source}"
    if effective.overridden:
        line += " — outranks " + ", ".join(effective.overridden)
    _pass(line)


def _rotation_free_state(provider: str, policy: str) -> tuple[bool, bool, str | None]:
    """The credentials that could cover *provider*: ``(api_key, env_token, stored)``.

    An exported API key outranks every other credential under the agent CLI's
    precedence AND never rotates, so it satisfies the rotation-free check — it
    also makes a `booley auth` token inert, so recommending one would steer the
    user to a credential the key overrides anyway.

    Under ``auth = "subscription"`` Booley scrubs the API keys from agent envs,
    so none of them can be the rotation-free credential. For Codex that covers
    its env/stored key too (its token IS an API key).
    """
    credential = auth_token.CREDENTIALS[provider]
    api_key_var = auth_token.API_KEY_ENV[provider]
    env_token = bool((os.environ.get(credential.env_var) or "").strip())
    stored = auth_token.read_stored_token(provider)
    api_key = api_key_var != credential.env_var and bool(
        (os.environ.get(api_key_var) or "").strip()
    )
    if policy == "subscription":
        api_key = False
        if credential.env_var == api_key_var:
            env_token = False
            stored = None
    return api_key, env_token, stored


def _check_oauth_token(
    provider: str,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    policy: str = "auto",
    *,
    _note: Check | None = None,
) -> None:
    """Report whether agents run on a rotation-free credential or a refreshing one.

    The default credential of BOTH apps refreshes, and refreshing ROTATES it: the
    host and the container hold copies of the same refresh token, so an unrelated
    agent session on the host can revoke a running container's copy and turn every
    in-flight agent into "Not logged in". The rotation-free alternative is
    per-app — Claude's one-year `claude setup-token`, Codex's API key — and
    neither can be revoked by a refresh elsewhere. Advisory: plenty of short runs
    are fine on subscription creds, but a multi-hour headless run is not.

    Stays a WARN (see :class:`_Reporter`) under automatic credential selection:
    it names a real, reachable failure mode with a one-command remedy. An
    explicit ``auth = "subscription"`` accepts that tradeoff, so it is a NOTE.
    Only the *other* provider's line is dropped -- see :func:`_configured_provider`.
    """
    note_sink = _note or _pass
    _warn = _warning_sink(_warn, "agent.rotation-risk", subject=provider)
    if not _agent_app_installed(provider):
        _skip(
            f"rotation-free credential check skipped - {_provider_not_installed_reason(provider)}"
        )
        return
    label = _AGENT_APP_LABELS[provider]
    credential = auth_token.CREDENTIALS[provider]
    api_key_var = auth_token.API_KEY_ENV[provider]
    api_key, env_token, stored = _rotation_free_state(provider, policy)

    if not api_key and not env_token and not stored:
        message = (
            f"{label}: no rotation-free credential; agents use a refreshing one, which a "
            "host-side login can revoke mid-run"
        )
        if policy == "subscription":
            note_sink(f"{message} (accepted by explicit auth = 'subscription')")
        else:
            _warn(message, f"booley auth --app {provider}")
        return
    # F-18: `stored` is truthy when the token comes from the read-only sidecar
    # seed too (the in-container case), but the privacy check only stats the
    # config-dir path. When that file is absent the mode is None → "not
    # private", firing an unfixable "chmod 600 <missing file>" WARN. Gate on the
    # config file actually existing (mode is not None), so the hint is only
    # shown where the chmod is real and actionable.
    if (
        stored
        and auth_token.stored_token_mode(provider) is not None
        and not auth_token.stored_token_is_private(provider)
    ):
        _warn(
            f"{label}: stored credential is readable by others: {auth_token.token_path(provider)}",
            f"chmod 600 {auth_token.token_path(provider)}",
        )
        return
    if api_key:
        note = f" — it outranks the stored {credential.label}" if env_token or stored else ""
        _pass(f"{label}: rotation-free API key configured ({api_key_var}, bills per token){note}")
        return
    source = credential.env_var if env_token else str(auth_token.token_path(provider))
    _pass(f"{label}: rotation-free {credential.label} configured ({source})")


def _claude_creds_expiry(path: Path) -> float | None:
    """``claudeAiOauth.expiresAt`` (epoch seconds) from *path*, or ``None``.

    ``None`` covers absent AND unparseable/field-less files — the presence
    checks elsewhere own those; this helper only feeds the expiry check.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expires_ms = data["claudeAiOauth"]["expiresAt"]
        return float(expires_ms) / 1000.0
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _check_subscription_creds_health(
    provider: str,
    _pass: Check,
    _warn: Check,
    policy: str = "auto",
) -> None:
    """A PRESENT subscription login can still be DEAD — check what's checkable.

    Claude's ``.credentials.json`` carries a machine-readable
    ``claudeAiOauth.expiresAt``; an expired one means the CLI must refresh at
    session start, and in-container that routinely fails (the host's own
    refresh ROTATES the shared refresh token; the 2026-07-23 incident wedged
    the file at ``expiresAt: 0`` and crashed every ticket at launch while
    doctor stayed green). In-container the seed SIDECAR is checked too: a
    single-file bind pins the host file's inode at container start, so after a
    host refresh the postStart re-copy re-seeds an already-dead snapshot.

    Only a WARN, and only when no rotation-free credential covers agent runs —
    with one present the creds file is an unused fallback (Codex has no
    comparable expiry field, so this is Claude-only).
    """
    _warn = _warning_sink(_warn, "agent.subscription-credential-expired", subject=provider)

    from booley.runtime import runtime_context

    if provider != auth_token.APP_CLAUDE or not _detect_claude_code():
        return
    app = auth_token.APP_CLAUDE
    rotation_free = bool(auth_token.resolve_token(app)) or (
        policy != "subscription" and bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    )

    now = time.time()
    creds_path = auth_token.subscription_creds_path(app)
    expiry = _claude_creds_expiry(creds_path)
    if expiry is not None and expiry <= now:
        when = (
            format_human_datetime(datetime.fromtimestamp(expiry).astimezone())
            if expiry > 0
            else "epoch 0"
        )
        if rotation_free:
            _pass(
                f"subscription login at {creds_path} is expired ({when}) — harmless: "
                "agents run on the rotation-free credential"
            )
        else:
            _warn(
                f"subscription login at {creds_path} is expired ({when}) and refresh "
                "fails once the host has rotated the shared refresh token — ticket "
                "agents then crash at launch",
                f"booley auth --app {app}  (or refresh the host login and recreate the container)",
            )
    elif expiry is not None:
        _pass(
            f"subscription login valid until "
            f"{format_human_datetime(datetime.fromtimestamp(expiry).astimezone())}"
        )

    if not runtime_context.inside_session_runtime():
        return
    seed_path = Path(dc._APP_CREDS_SEED_TARGET[app])
    seed_expiry = _claude_creds_expiry(seed_path)
    if seed_expiry is not None and seed_expiry <= now and not rotation_free:
        _warn(
            f"credentials seed {seed_path} is expired — the single-file bind is pinned "
            "to a pre-refresh inode, so every container (re)start re-seeds dead "
            "credentials",
            "recreate the container to re-bind the current host file — or store a "
            f"rotation-free credential: booley auth --app {app}",
        )


# Reload-Window guidance, shared by the two probe paths below. The manifest
# patch (booley.runtime.incontainer_vaporview) rewrites package.json from
# postAttachCommand, which VS Code runs *after* it has already launched the
# extension host — so on the first window of a fresh container the patch lands a
# beat too late and VaporView stays lazy, exactly as its module docstring warns.
# One reload fixes the window; the reload is what nobody knows to do.
_WCP_RELOAD_FIX = (
    "run 'Developer: Reload Window' in the attached VS Code window (the "
    "postAttach VaporView patch only takes effect at the next extension-host "
    "start); do not run 'WCP: Start Server' while auto-start is enabled -- "
    "VaporView 1.5.4 can bind the port during activation and then falsely "
    "report EADDRINUSE from a second start"
)

# Probe body: a bare TCP connect, run by whichever interpreter is at hand. Kept
# to connect_ex so a refused port is an exit code, never a traceback.
_WCP_PROBE_SOURCE = (
    "import socket,sys;"
    "s=socket.socket();s.settimeout(3);"
    "sys.exit(0 if s.connect_ex(('127.0.0.1',{port})) == 0 else 1)"
)

_VSCODE_EXTENSION_HOST_PROBE_SOURCE = (
    "from pathlib import Path;import sys;"
    "m=b'bootstrap-fork\\x00--type=extensionHost';hit=False;"
    "files=Path('/proc').glob('[0-9]*/cmdline');"
    "exec('for f in files:\n"
    " try:\n  hit = hit or m in f.read_bytes()\n"
    " except OSError:\n  pass');"
    "sys.exit(0 if hit else 1)"
)


def _wcp_port_listening(argv_prefix: list[str], port: int) -> bool | None:
    """True/False if the probe answered, None if it could not be run at all."""
    try:
        result = subprocess.run(
            [*argv_prefix, "python3", "-c", _WCP_PROBE_SOURCE.format(port=port)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    return result.returncode == 0


def _vscode_extension_host_running(argv_prefix: list[str]) -> bool | None:
    """True/False when a live VS Code extension host can be detected."""
    try:
        result = subprocess.run(
            [*argv_prefix, "python3", "-c", _VSCODE_EXTENSION_HOST_PROBE_SOURCE],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if result.returncode not in {0, 1}:
        return None
    return result.returncode == 0


def _check_wcp_server(
    project: ProjectAudit,
    docker_exe: str | None,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Live probe: is VaporView's WCP control server actually accepting connections?

    The spec check (:func:`_check_devcontainer_spec`) is static — it proves the
    devcontainer *asks* for the viewer, never that the viewer is up. A perfect
    spec still leaves the server dark on the first window of any fresh container
    (see :data:`_WCP_RELOAD_FIX`), and the failure surfaces only when a human
    finally runs `bwave gui` and gets "WCP server not running" from a doctor-clean
    project. This probe closes that gap by asking the port itself.

    From the host, scope the probe to a VS Code-created devcontainer: a headless
    `booley session up` container has no extension host by design. From inside a
    runtime, probe its own loopback because Docker metadata is unavailable there.
    """
    # Deferred, as everywhere else in this module: runtime location for the in-container
    # branch, and bwave_wcp for the port — importing the MCP-tool stack eagerly
    # would tax every doctor run for one integer.
    from booley.bwave import wcp as bwave_wcp
    from booley.runtime import runtime_context

    port = bwave_wcp.wcp_port()
    description = f"VaporView WCP server reachable on port {port} (`bwave gui`)"

    if runtime_context.inside_session_runtime():
        # Already inside the container: probe our own loopback, which is the
        # very socket `bwave gui` will dial.
        attached = _vscode_extension_host_running([])
        if attached is False:
            _skip(f"{description} - no VS Code extension host attached")
            return
        listening = _wcp_port_listening([], port)
        if listening is None:
            _skip(f"{description} - probe could not run")
        elif listening:
            _pass(description)
        else:
            _fail(f"{description}: nothing is listening", _WCP_RELOAD_FIX)
        return

    if not docker_exe:
        _skip(f"{description} - container runtime unavailable")
        return
    container = session_runtime.vscode_session_container(project.project_root)
    if container is None:
        _skip(f"{description} - no VS Code devcontainer running for this project")
        return
    argv_prefix = [docker_exe, "exec", container]
    attached = _vscode_extension_host_running(argv_prefix)
    if attached is False:
        _skip(f"{description} - no VS Code extension host attached in '{container}'")
        return
    listening = _wcp_port_listening(argv_prefix, port)
    if listening is None:
        _skip(f"{description} - probe could not run in '{container}'")
    elif listening:
        _pass(description)
    else:
        _fail(f"{description}: nothing is listening in '{container}'", _WCP_RELOAD_FIX)


def _check_interactive_docker_objects(
    docker_exe: str | None,
    _pass: Check,
    _warn: Check,
    _skip: Check,
) -> None:
    """ADR 0018: the long-lived egress network, proxy, and reaper are healthy."""
    if not docker_exe:
        _skip("interactive Docker objects check skipped - runtime unavailable")
        return
    if not idk.network_exists():
        _warning_sink(_warn, "interactive.docker-object-unhealthy", subject=idk.EGRESS_NETWORK)(
            f"{idk.EGRESS_NETWORK} network missing - run booley init"
        )
    elif idk.network_is_internal() and idk.network_is_host_isolated():
        _pass(f"{idk.EGRESS_NETWORK} network present (--internal, host-isolated)")
    else:
        _warning_sink(_warn, "interactive.docker-object-unhealthy", subject=idk.EGRESS_NETWORK)(
            f"{idk.EGRESS_NETWORK} is not both --internal and host-isolated; "
            "network or host-service access may leak - stop Sessions, remove the "
            "network and booley-proxy, then run booley init --force"
        )

    for name in (idk.PROXY_CONTAINER, idk.REAPER_CONTAINER):
        if idk.container_running(name):
            _pass(f"{name} running")
        elif idk.container_exists(name):
            _warning_sink(_warn, "interactive.docker-object-unhealthy", subject=name)(
                f"{name} exists but is stopped - docker start {name}"
            )
        else:
            _warning_sink(_warn, "interactive.docker-object-unhealthy", subject=name)(
                f"{name} missing - run booley init"
            )


def _check_interactive_state_volumes(
    project: ProjectAudit,
    docker_exe: str | None,
    verbose: bool,
    _pass: Check,
    _note: Check,
    _skip: Check,
) -> None:
    """Surface persistent home-state volumes and flag orphans for pruning.

    The named volumes that keep an interactive session's plans/transcripts alive
    across rebuilds persist by design; the reaper never removes them. They
    accumulate one-per-project, so list any belonging to *other* projects as
    prunable (a removed project leaves its volume behind).
    """
    if not docker_exe:
        _skip("interactive state-volume check skipped - runtime unavailable")
        return
    vols = idk.state_volumes()
    if not vols:
        _pass("no persistent interactive state volumes")
        return

    project_id = dc.canonical_project_id(project.project_root)
    mine = {dc.state_volume_name(app, project_id) for app in (dc.APP_CLAUDE, dc.APP_CODEX)}
    others = sorted(v for v in vols if v not in mine)

    present_mine = sorted(v for v in vols if v in mine)
    if present_mine:
        _pass(f"interactive state persists for this project ({', '.join(present_mine)})")

    if others:
        _note(
            f"{len(others)} interactive state volume(s) from other projects persist; "
            "remove unused ones with: docker volume rm <name>",
        )
        if verbose:
            for v in others:
                info(f"    {v}")
    elif not present_mine:
        # Volumes exist but the regex shape changed — surface the count anyway.
        _pass(f"{len(vols)} interactive state volume(s) present")


def _check_issued_image_keepers(
    project: ProjectAudit,
    docker_exe: str | None,
    verbose: bool,
    _pass: Check,
    _note: Check,
    _skip: Check,
) -> None:
    """Surface retained issuance images and possible keepers from old projects."""
    if not docker_exe:
        _skip("issued image-keeper check skipped - runtime unavailable")
        return
    tags = idk.issued_image_tags()
    if not tags:
        _pass("no retained Session Runtime image keepers")
        return

    from booley.eda import runtime_spec

    mine = runtime_spec.keeper_image(project.project_root)
    others = [tag for tag in tags if tag != mine]
    if mine in tags:
        _pass("issued Session Runtime image is retained for this Project")
    if others:
        _note(
            f"{len(others)} issued image keeper(s) from other projects persist; "
            "after confirming those projects are gone, remove one with: "
            "docker image rm <keeper-tag>"
        )
        if verbose:
            for tag in others:
                info(f"    {tag}")


def _run_mcp_probe(
    project: ProjectAudit,
    docker_exe: str,
    image: str,
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
) -> None:
    cmd = _mcp_probe_command(project, docker_exe, image)
    env = os.environ.copy()
    env["BOOLEY_PROJECT_DIR"] = str(project.project_dir)
    env["BOOLEY_MCP_MODE"] = "interactive"
    try:
        result = subprocess.run(
            cmd,
            cwd=project.project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=_MCP_PROBE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _fail(f"MCP server probe failed to start: {exc}", "rebuild sandbox image")
        return
    _check_mcp_probe_result(project, result, verbose, _pass, _warn, _fail)


def _mcp_probe_command(project: ProjectAudit, docker_exe: str, image: str) -> list[str]:
    return [
        docker_exe,
        "run",
        "--init",
        "--rm",
        "-v",
        f"{docker_mount_path(project.project_root)}:/work",
        "-w",
        "/work",
        "-e",
        "BOOLEY_PROJECT_DIR=/work/.booley_project",
        "-e",
        "BOOLEY_MCP_MODE=interactive",
        image,
        "python",
        "-c",
        _MCP_PROBE_PY,
    ]


def _check_mcp_probe_result(
    project: ProjectAudit,
    result: subprocess.CompletedProcess[str],
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
) -> None:
    if result.returncode != 0:
        _fail(f"MCP server probe failed with exit {result.returncode}", "rebuild sandbox image")
        _print_output_excerpt(result)
        return
    payload = _parse_mcp_probe_stdout(result.stdout)
    if payload is None:
        _fail("MCP server probe did not return valid JSON", "rebuild sandbox image")
        _print_output_excerpt(result)
        return
    _check_mcp_tool_payload(project, payload, verbose, _pass, _warn, _fail)


def _parse_mcp_probe_stdout(stdout: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _check_mcp_tool_payload(
    project: ProjectAudit,
    payload: dict[str, Any],
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
) -> None:
    _warn = _warning_sink(_warn, "interactive.mcp-tool-advisory-missing")
    mcp_tools = payload.get("tools")
    errors = payload.get("errors", [])
    if not is_str_list(mcp_tools) or not is_str_list(errors):
        _fail("MCP server probe returned malformed payload", "rebuild sandbox image")
        return
    if errors:
        _fail(f"MCP tool discovery reported {len(errors)} error(s)", errors[0])
        return
    if payload.get("logs_dir_ok") is not True:
        _fail("MCP interactive log setup failed", "check .booley_project mount and permissions")
        return
    missing = _required_mcp_tools(project) - set(mcp_tools)
    if missing:
        _fail(
            f"MCP missing required endpoint(s): {', '.join(sorted(missing))}",
            "fix [flows.<name>].enabled or reinstall Booley",
        )
        return
    _pass(f"MCP server exposes {len(mcp_tools)} MCP tool(s)")
    advisory_missing = _advisory_mcp_tools(project) - set(mcp_tools)
    if advisory_missing:
        _warn(
            f"MCP missing documented interactive endpoint(s): {', '.join(sorted(advisory_missing))}",
        )
    if verbose:
        info(f"    {', '.join(mcp_tools)}")


def _required_mcp_tools(project: ProjectAudit) -> set[str]:
    required = set(_BASE_REQUIRED_MCP_TOOLS)
    for flow_name in _AUDITED_FLOWS:
        if _flow_selection(project, flow_name).enabled:
            required.add(flow_name)
    if _elaborate_active(project):
        required.add("elab")
    return required


def _advisory_mcp_tools(project: ProjectAudit) -> set[str]:
    """Advisory interactive MCP tools that have not been explicitly disabled."""
    mcp_tools_cfg = project.booley_toml.get("mcp_tools", {})
    if not isinstance(mcp_tools_cfg, dict):
        mcp_tools_cfg = {}
    return {
        name
        for name in _ADVISORY_INTERACTIVE_MCP_TOOLS
        if config_section(mcp_tools_cfg, name).get("enabled") is not False
    }


def _run_preflight_parity_checks(
    project: ProjectAudit | None,
    reporter: _Reporter,
) -> None:
    """Mirror cheap run preflight checks in doctor output."""
    banner("Run checks")
    if project is None:
        reporter.skip_("run checks skipped - project config invalid")
        return

    _check_tickets_tree(project.project_dir, reporter.pass_, reporter.fail_)
    _check_git_state(project.project_root, reporter.pass_, reporter.note_, reporter.fail_)
    _check_repo_footprint(project.project_root, reporter.pass_, reporter.warn_)
    _check_ticket_board_import(project.project_root, reporter.pass_, reporter.fail_)
    _check_custom_endpoints_and_criteria(project.project_root, reporter.pass_, reporter.fail_)
    if reporter.agent_check_enabled("worker backend health check"):
        _check_agent_backend_health(
            project.project_root,
            reporter.pass_,
            reporter.warn_,
            _note=reporter.note_,
        )


def _check_tickets_tree(project_dir: Path, _pass: Check, _fail: Fail) -> None:
    tickets_dir = project_dir / "tickets"
    required = [tickets_dir / "board" / state for state in REQUIRED_BOARD_DIRS]
    required.extend([tickets_dir / "logs", tickets_dir / "locks"])
    missing = [path for path in required if not path.is_dir()]
    if missing:
        _fail(
            f"tickets tree missing {len(missing)} directories",
            "booley init",
        )
        return
    _pass("tickets tree present")


def _check_git_state(project_root: Path, _pass: Check, _note: Check, _fail: Fail) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _fail(f"git probe failed: {exc}", "install git and check repository state")
        return
    if result.returncode != 0:
        _fail("not inside a git work tree", "run doctor from the project repository")
        return

    _pass("git repository detected")
    _note_if_dirty(project_root, _note)
    _check_git_conflicts(project_root, _pass, _fail)


def _note_if_dirty(project_root: Path, _note: Check) -> None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--ignore-submodules"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    if result.returncode != 0:
        return
    dirty = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if dirty:
        _note(f"git working tree has {len(dirty)} modified file(s)")


def _check_git_conflicts(project_root: Path, _pass: Check, _fail: Fail) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    if result.returncode != 0:
        return
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = project_root / git_dir
    conflict_markers = {
        "merge": git_dir / "MERGE_HEAD",
        "rebase": git_dir / "rebase-merge",
        "rebase-apply": git_dir / "rebase-apply",
        "cherry-pick": git_dir / "CHERRY_PICK_HEAD",
    }
    active = [name for name, path in conflict_markers.items() if path.exists()]
    if active:
        _fail(f"git operation in progress: {', '.join(active)}", "resolve git state")
    else:
        _pass("no git merge/rebase/cherry-pick in progress")


# Booley-branded scaffolding filenames that must never be committed into a
# target repo. Booley's operational state lives under the gitignored
# `.booley_project/` dir (minimal-footprint / stealth rule, booley-setup
# SKILL.md); a tracked `*.booley.md` note means a setup agent leaked Booley
# scaffolding into the project's history. Leading `*` makes each git pathspec
# match at any depth (git wildcard pathspecs cross `/`).
_BOOLEY_FOOTPRINT_PATHSPECS = ("*.booley.md", "*.booley.txt", "*.booley.rst")


def _check_repo_footprint(project_root: Path, _pass: Check, _warn: Check) -> None:
    """Warn when Booley-branded scaffolding is committed into the target repo.

    Booley keeps all operational state under the gitignored `.booley_project/`
    dir, so a tracked `README.booley.md` / integration note means the setup
    agent broke the minimal-footprint rule and littered the project's history
    with Booley scaffolding. Warn (the repo still builds) with a `git rm
    --cached` remedy; notes belong under `.booley_project/`.
    """
    _warn = _warning_sink(_warn, "project.scaffolding-tracked")
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", *_BOOLEY_FOOTPRINT_PATHSPECS],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return  # not a git repo / git missing — footprint is unknowable, stay quiet
    if result.returncode != 0:
        return
    stray = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if stray:
        shown = ", ".join(stray[:5]) + (", …" if len(stray) > 5 else "")
        _warn(
            f"{len(stray)} Booley scaffolding file(s) committed into the repo "
            f"({shown}) — Booley state belongs under the gitignored "
            ".booley_project/; move any notes there and `git rm --cached` the "
            "strays (booley-setup SKILL.md: minimal-footprint rule)"
        )
    else:
        _pass("no Booley scaffolding files tracked in the repo")


def _check_ticket_board_import(project_root: Path, _pass: Check, _fail: Fail) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import booley.ticket_board"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _fail(f"ticket_board import probe failed: {exc}", "reinstall Booley")
        return
    if result.returncode == 0:
        _pass("ticket_board package importable")
    else:
        detail = result.stderr.strip() or result.stdout.strip() or "import failed"
        _fail(f"ticket_board package not importable: {detail}", "reinstall Booley")


def _check_custom_endpoints_and_criteria(project_root: Path, _pass: Check, _fail: Fail) -> None:
    try:
        from booley.harness.preflight import (
            PreflightError,
            _validate_custom_endpoints_and_criteria,
        )

        _validate_custom_endpoints_and_criteria(project_root)
    except PreflightError as exc:
        _fail("custom endpoint/Criteria validation failed", exc.failures[0])
        return
    except (ImportError, OSError, ValueError) as exc:
        _fail(
            f"custom endpoint/Criteria validation failed: {exc}",
            "fix .booley_project/mcp_tools and Criteria configuration",
        )
        return
    _pass("custom endpoints and Criteria validated")


def _check_agent_backend_health(
    project_root: Path,
    _pass: Check,
    _warn: Check,
    *,
    _note: Check | None = None,
) -> None:
    _warn = _warning_sink(_warn, "agent.backend-health")
    try:
        from booley.config.settings import get_backend_config, load_models_config

        load_models_config(project_root)
        cfg = get_backend_config()
        warning = cfg.active_backend.health_check()
    except (ImportError, AttributeError, RuntimeError, OSError, ValueError) as exc:
        _warn(f"worker backend health check failed: {exc}")
        return
    if warning:
        _warn(f"worker backend ({cfg.active_backend.name}): {warning}")
    else:
        note_sink = _note or _pass
        note_sink(
            f"worker backend configured locally: {cfg.active_backend.name}; "
            "provider authorization was not exercised by plain Doctor "
            "(run `booley doctor --deep` for a live check)"
        )


def _check_design_size(project: ProjectAudit, _pass: Check, _note: Check) -> None:
    """Advise when the design is large enough to strain ``--deep`` budgets (F5).

    ``--deep``'s asic/sim smokes can OOM (a big flatten on a memory-tight host)
    or run long. ``_deep_timeout_s`` already raises the wall-clock budget from a
    project's ``timeout_ms``, but nothing says so *before* opting into ``--deep``,
    and a raised timeout does not help an OOM. Set expectations up front instead
    of surfacing a scary spurious FAIL after a 30-minute smoke.

    A NOTE, not a WARN: a big design is a fact about the project, not a defect.
    Nothing here is broken and there is nothing to fix -- the size only sets
    expectations for ``--deep``.
    """
    audit = design_size.analyze_design_size(
        project.project_root,
        project.project_dir,
        _project_target_matrix(project).seed_targets,
    )
    label = (
        "Doctor Target filesets"
        if audit.scope is design_size.DesignSizeScope.CONFIGURED_TARGETS
        else "whole-repository estimate"
    )
    files = audit.hdl_files
    loc = audit.lines_of_code
    if audit.exceeds_deep_smoke_budget:
        _note(
            f"large design ({label}: ~{files} HDL files / ~{loc:,} LOC): --deep's smoke "
            "checks may run long or OOM (asic flatten especially). Validate heavy "
            "flows manually with a raised --timeout, and set "
            "[flows.<flow>].timeout_ms so --deep honors a larger budget."
        )
    else:
        _pass(
            f"design size ({label}: ~{files} HDL files / ~{loc:,} LOC) within --deep smoke budgets"
        )


def _run_flow_audit(
    project: ProjectAudit,
    docker_exe: str | None,
    verbose: bool,
    _pass: Check,
    _note: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Validate enabled Flows and run Session Runtime dry-run smoke checks."""
    _check_design_size(project, _pass, _note)
    for flow_name in _AUDITED_FLOWS:
        selection = _flow_selection(project, flow_name)
        if not selection.enabled:
            _skip(f"{flow_name} disabled in booley.toml")
            continue
        execution_error = _execution_error(flow_name, selection)
        if execution_error:
            # Includes the ADR 0037 hard-migration error for retired combined
            # backend spellings — the message carries the exact replacement lines.
            _fail(execution_error, f"fix [flows.{flow_name}] configuration")
            continue
        _pass(f"{flow_name} executes in the Session Runtime")
        # Pre-Run Commands (ADR 0039): a true observation about healthy config —
        # the lines run inside the Session Runtime before every sim run, so their
        # cost and side effects are worth a heads-up in the audit.
        if flow_name == "sim":
            flows_tbl = project.booley_toml.get("flows", {})
            sim_tbl = flows_tbl.get("sim", {}) if isinstance(flows_tbl, dict) else {}
            pre_run = sim_tbl.get("pre_run_commands") if isinstance(sim_tbl, dict) else None
            if pre_run:
                _note(
                    f"sim pre_run_commands configured ({len(pre_run)} "
                    f"line(s)); they run in the Session Runtime before "
                    "each sim run (BOOLEY_* env contract, ADR 0039)"
                )
        targets = _check_doctor_targets(project, flow_name, _fail)
        if not targets:
            continue
        _check_flow_runtime_reality(
            project,
            flow_name,
            targets,
            docker_exe=docker_exe,
            _pass=_pass,
            _skip=_skip,
            _fail=_fail,
        )
        for target in targets:
            _run_flow_check(
                project,
                flow_name,
                selection,
                target=target,
                dry_run=True,
                docker_exe=docker_exe,
                # The fusesoc roots scan alone can exceed 60s on large repos.
                timeout_s=_configured_timeout_s(project, flow_name, _DRY_RUN_TIMEOUT_S),
                verbose=verbose,
                _pass=_pass,
                _warn=_warn,
                _skip=_skip,
                _fail=_fail,
            )

    _check_elaborate_setup(project, _pass, _skip, _fail)


def _check_flow_runtime_reality(
    project: ProjectAudit,
    flow_name: str,
    targets: list[str],
    *,
    docker_exe: str | None,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Probe every selected Target's EDA binary in the Session Runtime."""
    for binary in _runtime_probe_binaries(project, targets):
        _check_sandbox_binary(
            flow_name,
            binary,
            docker_exe=docker_exe,
            image=_sandbox_image(project),
            _pass=_pass,
            _skip=_skip,
            _fail=_fail,
        )


_EDA_TOOL_BINARIES = {
    "verilator": "verilator",
    "icarus": "iverilog",
    "yosys": "yosys",
    "vivado": "vivado",
    "verible": "verible-verilog-lint",
}


def _runtime_probe_binaries(project: ProjectAudit, targets: list[str]) -> list[str]:
    """Return the distinct runtime executables required by selected Targets."""
    binaries: list[str] = []
    for target in targets:
        try:
            ref = fusesoc_registry.resolve_ref(project.project_root, target)
        except fusesoc_registry.FuseSocError:
            continue
        binary = _EDA_TOOL_BINARIES.get((ref.eda_tool or "").lower())
        if binary and binary not in binaries:
            binaries.append(binary)
    return binaries


def _check_sandbox_binary(
    flow_name: str,
    binary: str,
    *,
    docker_exe: str | None,
    image: str,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """PASS/FAIL on *binary* being on the Session Runtime PATH."""
    from booley.runtime import runtime_context

    label = f"{flow_name}: '{binary}' on the Session Runtime PATH"
    if runtime_context.inside_session_runtime():
        # This container IS the sandbox (ADR 0028) — probe the local PATH.
        if shutil.which(binary):
            _pass(label)
        else:
            _fail(
                f"{flow_name}: '{binary}' is not on this container's PATH",
                f"bake {binary} into the Session Runtime image and rebuild (booley init --force)",
            )
        return
    if not docker_exe:
        _skip(f"{label} - container runtime unavailable")
        return
    if not _docker_image_exists_by_name(image):
        # The missing image is already a FAIL in the container checks.
        _skip(f"{label} - sandbox image '{image}' unavailable")
        return
    try:
        result = subprocess.run(
            [docker_exe, "run", "--rm", image, "sh", "-c", f"command -v {binary}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        _fail(f"{label} (probe timeout/error)", "rebuild the sandbox image")
        return
    if result.returncode == 0:
        _pass(label)
    else:
        _fail(
            f"{flow_name}: '{binary}' is not on the '{image}' image's PATH",
            f"bake {binary} into the Session Runtime image and rebuild (booley init --force)",
        )


def _owned_core_files(project: ProjectAudit, root: Path) -> set[Path]:
    """The ``.core`` files whose findings the project can act on (ADR 0036).

    A core is "owned" when it hosts a Target selected by Doctor metadata (the
    project drives it, so its defects are the project's problem) or lives in the
    state zone (``.booley_project/cores/`` is always Booley-authored). Everything
    else is vendored upstream content — advice like "fix the .core" is
    unfollowable for a repo that must stay byte-identical to upstream.
    """
    owned: set[Path] = set()
    for token in _project_target_matrix(project).seed_targets:
        try:
            owned.add(fusesoc_registry.resolve_ref(root, token).core_file)
        except fusesoc_registry.FuseSocError:
            continue  # unresolvable config tokens are their own doctor finding
    state_cores = fusesoc_registry.state_cores_dir(root)
    for core_file in fusesoc_registry.discover_cores(root):
        if state_cores in core_file.parents:
            owned.add(core_file)
    return owned


def _selected_core_targets(project: ProjectAudit, root: Path) -> set[tuple[Path, str]]:
    """Return the exact ``(core_file, Target)`` pairs selected by Doctor metadata.

    A native core can contain dozens of historical board and example Targets
    while Booley selects only one newly modernized Target from it.  Core-level
    ownership is therefore too broad for modernization advice: selecting one
    Target must not turn every unrelated declaration in that file into a WARN.
    State-zone cores remain wholly Booley-authored and are handled separately by
    each caller.
    """
    selected: set[tuple[Path, str]] = set()
    for token in _project_target_matrix(project).seed_targets:
        try:
            ref = fusesoc_registry.resolve_ref(root, token)
        except fusesoc_registry.FuseSocError:
            continue  # target-resolution diagnostics report this elsewhere
        selected.add((ref.core_file, ref.name))
    return selected


def _check_core_schema(
    project: ProjectAudit,
    root: Path,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
) -> None:
    """Flag CAPI2 array-field schema violations in any discovered ``.core``.

    Booley's tolerant ``yaml.safe_load`` accepts a scalar — or a bare ``depend:``
    that parses to ``None`` — where FuseSoC's schema demands an array; cheap
    enumeration then greenlights a ``.core`` the deep resolve later rejects with
    ``... must be array``. Reproduce that verdict host-side (no subprocess).

    Severity follows ownership. A violation FAILs when the core is one the
    project actually drives — it hosts a Doctor-selected Target — or
    lives in the state zone (``.booley_project/cores/``, always Booley-authored,
    ADR 0036). A vendored upstream core the project never selects (the pristine
    YosysHQ ``picorv32.core`` ships a bare ``depend:``) is a WARN instead:
    FuseSoC drops such a core at resolve time, so only *its own* targets are
    unusable — the configured flows are unaffected, and "fix the .core" is
    unfollowable advice for a repo that must stay byte-identical to upstream.
    """
    note_sink = _note or _pass
    owned_cores = _owned_core_files(project, root)
    schema_clean = True
    for core_file in fusesoc_registry.discover_cores(root):
        errors = fusesoc_registry.core_schema_errors(core_file)
        if not errors:
            continue
        schema_clean = False
        owned = core_file in owned_cores
        for msg in errors:
            if owned:
                _fail(
                    f".core schema {core_file.name}: {msg}",
                    "fix the .core — FuseSoC's CAPI2 schema requires this "
                    "(caught here so it does not only surface under --deep)",
                )
            else:
                note_sink(
                    f".core schema {core_file.name}: {msg} — vendored core, no "
                    "Doctor Target selects it; FuseSoC skips it at resolve, "
                    "so only that core's own Targets are unusable"
                )
    if schema_clean:
        _pass(".core CAPI2 array-field schema valid")


def _check_core_setup_hazards(
    project: ProjectAudit,
    root: Path,
    _pass: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
) -> None:
    """Catch offline-provider and recursive-symlink failures before FuseSoC."""
    note_sink = _note or _pass
    owned_cores = _owned_core_files(project, root)
    selected_closure = fusesoc_registry.selectable_core_closure(
        root, _project_target_matrix(project).seed_targets
    )
    required_cores = owned_cores | set(selected_closure or ())
    hazards = fusesoc_registry.core_setup_hazards(root)
    if not hazards:
        _pass(".core tree has no provider or recursive-symlink setup hazards")
        return
    for hazard in hazards:
        rel = hazard.path.relative_to(root)
        if hazard.kind == "recursive-symlink":
            _fail(
                f"FuseSoC recursive symlink {rel}: {hazard.detail}",
                "add a FUSESOC_IGNORE marker to the containing subtree or remove the link",
            )
        elif hazard.path in required_cores:
            _fail(
                f"in-tree .core {rel} has a provider block that requests a remote fetch",
                "remove provider: from the in-tree core",
            )
        else:
            note_sink(
                f"vendored .core {rel} has a provider block, but no Doctor Target selects it"
            )


def _doctor_target_incompatibility(
    target: str,
    flow_name: str,
    ref: fusesoc_registry.TargetRef,
) -> tuple[str, str] | None:
    """Return the diagnostic for an invalid Doctor Target/Flow pairing."""
    from booley.targets.target_surface import flow_can_drive

    if flow_can_drive(flow_name, ref):
        return None
    return (
        f"Target {target!r} selects incompatible Doctor Flow {flow_name!r} "
        f"(CAPI2 flow={ref.flow or '?'}, EDA tool={ref.eda_tool or '?'})",
        f"remove {flow_name!r} from flow_options.booley.doctor or fix the "
        "Target's `flow` and `flow_options.tool` fields",
    )


def _check_doctor_target_compatibility(root: Path, _pass: Check, _fail: Fail) -> None:
    """Validate every explicit Doctor Target/Flow pairing."""
    declarations = fusesoc_registry.target_declarations(root)
    selected = 0
    for name, refs in declarations.items():
        for ref in refs:
            for flow_name in ref.doctor_flows:
                selected += 1
                failure = _doctor_target_incompatibility(name, flow_name, ref)
                if failure:
                    _fail(*failure)
    if selected:
        _pass(f"Doctor Target matrix valid: {selected} Target/Flow pair(s)")
    else:
        _pass("Doctor Target matrix is empty")


def _check_sim_target_setup(
    project: ProjectAudit,
    inputs: _CoreAuditInputs,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
    note_sink: Check,
) -> None:
    """Run source-backed setup checks for every simulation Target."""
    for name, ref in sorted(inputs.refs.items()):
        if ref.flow != "sim":
            continue
        sources = inputs.sources_for(name)
        _check_sim_target_tb_staging(name, sources, _pass, _fail)
        _check_sim_verdict_setup(
            project,
            project.project_root,
            name,
            ref,
            _pass,
            _warn,
            sources=sources,
            _note=note_sink,
        )


def _enumerate_core_audit_targets(
    root: Path,
    _pass: Check,
    _fail: Fail,
) -> dict[str, fusesoc_registry.TargetRef] | None:
    """Enumerate selectable Targets, reporting structural failures."""
    try:
        refs = fusesoc_registry.enumerate_targets(root)
    except fusesoc_registry.FuseSocError as exc:
        _fail(f".core enumeration failed: {exc}", "fix the malformed .core")
        return None
    if not refs:
        _fail(
            "project has no .core: a resolvable FuseSoC .core Target is a "
            "precondition for every Booley Flow (ADR 0039)",
            "author a .core (the booley-setup skill's project-config step walks through it)",
        )
    else:
        _pass(f".core Targets enumerated: {', '.join(sorted(refs))}")
    return refs


@dataclass(frozen=True)
class _CoreAuditContext:
    """Project and reporting sinks shared by one authored-core audit."""

    project: ProjectAudit
    pass_: Check
    warn: Check
    skip: Check
    fail: Fail
    note: Check

    @property
    def root(self) -> Path:
        """Return the audited project root."""
        return self.project.project_root


def _check_target_metadata(
    audit: _CoreAuditContext,
    refs: dict[str, fusesoc_registry.TargetRef],
) -> None:
    """Check Target declarations that do not need source partitions."""
    _check_doctor_target_compatibility(audit.root, audit.pass_, audit.fail)
    _check_legacy_core_targets(
        audit.project, audit.root, refs, audit.pass_, audit.warn, _note=audit.note
    )
    _check_naming_conventions(
        audit.project, audit.root, refs, audit.pass_, audit.warn, _note=audit.note
    )
    _check_yosys_targets_have_arch(audit.root, refs, audit.pass_, audit.warn)
    _check_sim_traceable(audit.root, refs, audit.pass_, audit.warn)
    _check_cocotb_targets(audit.project, refs, audit.pass_, audit.warn, audit.skip, audit.fail)


def _check_target_sources(
    audit: _CoreAuditContext,
    refs: dict[str, fusesoc_registry.TargetRef],
    inputs: _CoreAuditInputs,
) -> None:
    """Run Target checks that share source partitions."""
    _check_sim_target_setup(audit.project, inputs, audit.pass_, audit.warn, audit.fail, audit.note)
    _check_icarus_sv_language_mode(
        audit.root,
        refs,
        audit.pass_,
        audit.warn,
        audit.fail,
        _note=audit.note,
        selected_targets=set(_project_target_matrix(audit.project).seed_targets) or None,
        _inputs=inputs,
    )
    _check_toplevel_interface_ports(audit.root, refs, audit.pass_, audit.warn, _inputs=inputs)


def _check_core_repository_hygiene(audit: _CoreAuditContext) -> None:
    """Check that authored core inputs are represented safely in Git."""
    _check_core_files_tracked(audit.root, audit.pass_, audit.warn)
    _check_readmemh_targets_tracked(audit.root, audit.pass_, audit.warn)
    _check_committed_build_artifacts(audit.root, audit.pass_, audit.warn)


def _run_core_security_audit(
    audit: _CoreAuditContext,
    refs: dict[str, fusesoc_registry.TargetRef],
) -> None:
    """Report authored-core provenance and confinement violations."""
    violations = core_security.validate_project_cores(
        audit.root,
        scope=_project_write_scope(audit.root),
        seed_targets=_project_target_matrix(audit.project).seed_targets,
    )
    for violation in violations:
        target = f" target '{violation.target}'" if violation.target else ""
        line = (
            f".core security [{violation.kind}] {violation.core_file.name}"
            f"{target}: {violation.message}"
        )
        if violation.kind in _SCOPE_DEPENDENT_VIOLATIONS:
            _warning_sink(
                audit.warn,
                "core.security-scope-advisory",
                subject=f"{violation.core_file.name}:{violation.target or '-'}",
            )(
                f"{line} (advisory: doctor audits the union of writable "
                "category dirs, not a real ticket Scope — the binding "
                "check runs per-ticket at commit time)"
            )
        else:
            audit.fail(line, "ADR 0022 decision 21")
    if not violations and refs:
        audit.pass_(
            ".core security validation passed (no fpga hooks / expr-params / in-scope scripts)"
        )


def _run_core_audit(
    project: ProjectAudit,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
) -> None:
    """Coordinate the structural, security, and metadata core audits."""
    audit = _CoreAuditContext(project, _pass, _warn, _skip, _fail, _note or _pass)
    _check_core_setup_hazards(project, audit.root, _pass, _fail, _note=audit.note)
    _check_core_schema(project, audit.root, _pass, _warn, _fail, _note=audit.note)
    refs = _enumerate_core_audit_targets(audit.root, _pass, _fail)
    if refs is None:
        return
    if refs:
        inputs = _CoreAuditInputs(audit.root, refs)
        _check_target_metadata(audit, refs)
        _check_target_sources(audit, refs, inputs)
        _check_core_repository_hygiene(audit)
    _run_core_security_audit(audit, refs)
    _audit_tests_toml(project, _pass, _skip, _fail)
    _audit_native_dependencies(project, _pass, _warn)


def _check_sim_target_tb_staging(
    name: str,
    sources: fusesoc_registry.CoreSources,
    _pass: Check,
    _fail: Fail,
) -> None:
    """Fail a sim Target whose staged fileset carries no usable testbench.

    Two shapes of the same defect — a sim Target that cannot produce a
    verdict, caught at setup time instead of as a runtime error blamed on
    the design:

    - files staged but none tagged ``tags:[tb]`` (ADR 0022 dec 13): Source
      Isolation would mis-classify the TB as RTL (mutation_tester would
      mutate it).
    - ZERO files staged at all: the untagged-TB predicate needs RTL files to
      fire, so a sim Target whose filesets list stages nothing (e.g. the tb
      fileset dropped from ``filesets:``) slipped past it and died at run
      time with a toplevel-not-found error pointing at the design.
    """
    if sources.rtl_source_files and not sources.tb_files:
        _fail(
            f"sim Target '{name}' has files but none tagged tags:[tb]",
            "tag the testbench fileset tags:[tb] so Source Isolation can "
            "partition RTL vs TB (ADR 0022 dec 13)",
        )
    elif not sources.tb_files:
        _fail(
            f"sim Target '{name}' stages zero files — no testbench "
            "(or design) ever reaches the simulator",
            "list the rtl + tb filesets in the Target's filesets: "
            "entry (the tb fileset tagged tags:[tb], ADR 0022 dec 13)",
        )
    else:
        _pass(f"sim Target '{name}' TB fileset tagged")


def _sim_pass_sentinels(project: ProjectAudit) -> tuple[list[str], bool]:
    """Return configured verdict sentinels and whether they were explicit."""
    flows = project.booley_toml.get("flows", {})
    sim_cfg = flows.get("sim", {}) if isinstance(flows, dict) else {}
    sentinels = [str(s) for s in (sim_cfg.get("pass_sentinels") or [])] or ["[SIM_RESULT] PASSED"]
    configured = isinstance(sim_cfg, dict) and bool(sim_cfg.get("pass_sentinels"))
    return sentinels, configured


def _tb_emits_sentinel(root: Path, tb_files: tuple[str, ...], sentinels: list[str]) -> bool:
    """Return whether any readable TB source contains a verdict sentinel."""
    for rel in tb_files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(sentinel in text for sentinel in sentinels):
            return True
    return False


def _warn_missing_sim_sentinel(
    name: str,
    tb_files: tuple[str, ...],
    shown: str,
    _warn: Check,
) -> None:
    """Explain the correct sentinel remedy for HDL and Python testbenches."""
    emit = _warning_sink(_warn, "sim.pass-sentinel-missing", subject=name)
    if any(Path(rel).suffix == ".py" for rel in tb_files):
        emit(
            f"sim Target '{name}' TB emits no configured pass sentinel ({shown}) "
            "and its TB fileset is Python — for a cocotb TB declare "
            "flow_options.cocotb_module on the Target (ADR 0034) so the verdict "
            "comes from results.xml; $display sentinels only apply to SV TBs"
        )
        return
    emit(
        f"sim Target '{name}' TB emits no configured pass sentinel ({shown}) "
        "— a passing run will read as INCONCLUSIVE; add a $display sentinel "
        "(refs/sim_result_sentinel.sv) or set [flows.sim].pass_sentinels"
    )


def _check_sim_verdict_setup(
    project: ProjectAudit,
    root: Path,
    name: str,
    ref: fusesoc_registry.TargetRef,
    _pass: Check,
    _warn: Check,
    *,
    sources: fusesoc_registry.CoreSources | None = None,
    _note: Check | None = None,
) -> None:
    """Check that a non-cocotb simulation TB can emit a pass verdict."""
    if ref.cocotb_module:
        return
    sentinels, configured = _sim_pass_sentinels(project)
    if sources is None:
        try:
            sources = fusesoc_registry.target_source_files(root, name)
        except fusesoc_registry.FuseSocError:
            return
    tb_files = sources.tb_files
    if not tb_files:
        return  # the untagged-TB check already covered "sim Target has no TB"
    if _tb_emits_sentinel(root, tb_files, sentinels):
        _pass(f"sim Target '{name}' TB emits a recognized pass sentinel")
        return
    shown = ", ".join(repr(sentinel) for sentinel in sentinels)
    if configured:
        (_note or _pass)(
            f"sim Target '{name}' uses explicitly configured pass sentinel(s) ({shown}); "
            "none appears literally in its tagged TB sources, so validate the runtime "
            "producer with this Target's simulation"
        )
        return
    _warn_missing_sim_sentinel(name, tb_files, shown, _warn)


def _check_legacy_core_targets(
    project: ProjectAudit,
    root: Path,
    refs: dict,
    _pass: Check,
    _warn: Check,
    *,
    _note: Check | None = None,
) -> None:
    """Warn on Targets authored with legacy FuseSoC ``default_tool``/``tools`` fields.

    Such a Target declares no ``flow``. Booley can still classify it from the
    legacy ``default_tool`` family, but the intent is ambiguous for EDA tools such
    as Verilator that support both sim and lint. Name the modernization for
    owned cores; keep vendored upstream cores advisory-only.
    """
    note_sink = _note or _pass
    del refs  # audit every declaration, including duplicate bare Target names
    state_cores = fusesoc_registry.state_cores_dir(root)
    selected = _selected_core_targets(project, root)
    legacy = []
    try:
        declarations = fusesoc_registry.target_declarations(root)
    except fusesoc_registry.FuseSocError:
        return  # the structural audit reports enumeration failures
    docs: dict[Path, Mapping[str, Any]] = {}
    for name, bucket in sorted(declarations.items()):
        for ref in bucket:
            try:
                doc = docs.setdefault(ref.core_file, fusesoc_registry.read_core(ref.core_file))
            except fusesoc_registry.FuseSocError:
                continue
            if fusesoc_registry.core_target_uses_legacy_fusesoc_api(doc, name):
                legacy.append((name, ref))
    if not legacy:
        _pass(".core Targets use the flow API (no legacy FuseSoC tools-section targets)")
        return
    # A configured native Target gets a per-Target WARN because the project
    # actively drives it.  Every Target in the hidden state-zone adapter is
    # Booley-authored and gets the same treatment.  Unselected native Targets
    # are modernization context only: roll them up per core instead of making a
    # large legacy core fail the setup gate merely because one sibling Target
    # was selected.
    native_unselected: dict[Path, list[str]] = {}
    for name, ref in legacy:
        is_state_core = state_cores in ref.core_file.parents
        if is_state_core or (ref.core_file, name) in selected:
            _warning_sink(_warn, "core.legacy-target", subject=name)(
                f"Target '{name}' in {ref.core_file.name} is a legacy FuseSoC tools-section "
                f"target (FuseSoC default_tool/tools fields, no flow) — Booley falls back to "
                f"the declared EDA-tool family, but the Target's intent is ambiguous; "
                f"rewrite it to the flow API: flow: <sim|lint|synth> + "
                f"flow_options: {{tool: {ref.eda_tool or '<eda-tool>'}}} (see CORE_TEMPLATE.yaml)"
            )
        else:
            native_unselected.setdefault(ref.core_file, []).append(name)
    for core_file, names in native_unselected.items():
        listed = ", ".join(sorted(names))
        plural = "targets" if len(names) > 1 else "target"
        note_sink(
            f"{core_file.name}: {len(names)} legacy FuseSoC tools-section {plural} ({listed}) "
            f"— Booley falls back to each declared EDA-tool family; these Targets "
            f"are not selected by this project, so modernizing them is optional"
        )


def _check_naming_conventions(
    project: ProjectAudit,
    root: Path,
    refs: dict,
    _pass: Check,
    _warn: Check,
    *,
    _note: Check | None = None,
) -> None:
    """Both Target-name advisories: the ``<axis>_<subject>`` rule and dead ``default:``."""
    _check_target_naming(project, root, refs, _pass, _note or _pass)
    _check_stray_default_targets(project, root, _pass, _warn, _note=_note)


def _check_target_naming(
    project: ProjectAudit,
    root: Path,
    refs: dict,
    _pass: Check,
    _note: Check,
) -> None:
    """Note Booley-authored Targets that break the ``<axis>_<subject>`` convention.

    The Target name is the only place ``synth`` vs ``fpga`` is written down (CAPI2
    has no synth flow — both resolve as ``generic``), and it is what an agent
    reads in ``--target``, ``sim_pass_<target>``, and ``tests.toml`` keys. See
    :mod:`booley.targets.target_naming` for the rule.

    WARN, never FAIL, and only for owned cores (:func:`_owned_core_files`): a
    non-conforming name still runs, and renaming a vendored upstream Target is
    unfollowable advice for a repo that must stay byte-identical (ADR 0036).
    """
    del refs  # qualified duplicate Target names need exact declaration scope
    state_cores = fusesoc_registry.state_cores_dir(root)
    selected = _selected_core_targets(project, root)
    doctor_axes = _project_target_matrix(project).axes()
    try:
        declarations = fusesoc_registry.target_declarations(root)
    except fusesoc_registry.FuseSocError:
        return
    offenders = [
        (name, ref)
        for name, bucket in sorted(declarations.items())
        for ref in bucket
        if (state_cores in ref.core_file.parents or (ref.core_file, name) in selected)
        and target_naming.violation(name)
    ]
    if not offenders:
        _pass("Target names follow the <axis>_<subject> convention")
        return
    for name, ref in offenders:
        suggestion = target_naming.suggest_name(name, doctor_axes.get(name))
        fix = (
            f"rename it '{suggestion}'"
            if suggestion
            else f"prefix it with the driving Booley Flow's axis ({'/'.join(target_naming.TARGET_AXES)})"
        )
        _note(
            f"Target '{name}' in {ref.core_file.name}: {target_naming.violation(name)} "
            f"— {fix} (renaming also touches tests.toml keys and any ticket "
            "criteria naming it)"
        )


def _check_stray_default_targets(
    project: ProjectAudit,
    root: Path,
    _pass: Check,
    _warn: Check,
    *,
    _note: Check | None = None,
) -> None:
    """Warn on a ``default:`` Target in an owned core that nothing depends on.

    ``default`` is FuseSoC's fallback for a core built as a *dependency*
    (``_get_target`` selects it for any non-toplevel core), and Booley filters it
    out of the selectable surface (decision 10) — so on a core with no
    dependents it is a Target nobody can ever ``--target``, quietly drifting out
    of sync with the real ones. Delete it there.

    On a core that *is* depended upon, ``default`` is load-bearing in the
    opposite direction: without it FuseSoC contributes zero filesets from that
    core, silently. Those are left alone.
    """
    _warn = _warning_sink(_warn, "core.stray-default-target")
    note_sink = _note or _pass
    state_cores = fusesoc_registry.state_cores_dir(root)
    owned_cores = _owned_core_files(project, root)
    if not owned_cores:
        return
    depended = fusesoc_registry.depended_on_core_keys(root)
    stray: list[tuple[Path, bool]] = []
    for core_file in sorted(owned_cores):
        try:
            doc = fusesoc_registry.read_core(core_file)
        except fusesoc_registry.FuseSocError:
            continue
        targets = doc.get("targets")
        if not isinstance(targets, Mapping) or "default" not in targets:
            continue
        if fusesoc_registry.core_identity_key(doc) not in depended:
            stray.append((core_file, state_cores in core_file.parents))
    if not stray:
        _pass("no dead 'default:' Targets in Booley-authored .core files")
        return
    for core_file, is_state_core in stray:
        message = (
            f"{core_file.name} declares a 'default:' Target but no other .core "
            "depends on it — 'default' is FuseSoC's dependency-build fallback and "
            "is filtered out of Booley's selectable surface, so nothing can ever "
            "select it; delete it, or give it a real <axis>_<subject> name"
        )
        if is_state_core:
            _warn(message)
        else:
            note_sink(message + " (native core; optional modernization)")


def _check_yosys_targets_have_arch(
    root: Path,
    refs: dict,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when a Yosys synth Target omits the mandatory ``flow_options.arch``.

    edalize's Yosys backend requires an ``arch`` option (it selects the
    ``synth_<arch>`` pass); a ``.core`` that drops it is valid CAPI2 and passes
    enumeration, then fails only at ``--deep`` resolution with the opaque
    ``yosys requires tool option 'arch'``. A warn (not fail): the ``.core`` is
    structurally sound and Booley's own synth script overrides the pass, but the
    field must be present for edalize to configure. Reads flow_options host-side.
    """
    yosys_targets = [
        (name, ref)
        for name, ref in sorted(refs.items())
        if (ref.eda_tool or "").lower() == "yosys"
    ]
    if not yosys_targets:
        return
    for name, ref in yosys_targets:
        try:
            doc = fusesoc_registry.read_core(ref.core_file)
        except fusesoc_registry.FuseSocError:
            continue
        arch = fusesoc_registry.core_target_flow_option(doc, name, "arch")
        if arch:
            _pass(f"synth Target '{name}' (yosys) declares flow_options.arch")
        else:
            _warning_sink(_warn, "core.yosys-arch-missing", subject=name)(
                f"synth Target '{name}' (yosys) has no flow_options.arch — "
                "edalize's yosys backend requires it and --deep resolution will "
                "fail with \"yosys requires tool option 'arch'\"; add e.g. "
                "flow_options: {tool: yosys, arch: xilinx}"
            )


# iverilog language-generation flags that enable SystemVerilog parsing.
# iverilog's DEFAULT generation is IEEE1364-2005 (plain Verilog), so a .sv
# fileset compiled without one of these dies on the first `logic`/`always_ff`
# with a bare syntax error that reads like a design bug — a staging defect
# that has manufactured whole benchmark failures. -g2012 is the one Booley
# recommends; the older SV-enabling spellings are accepted so a working .core
# is not nagged into churn.
_ICARUS_SV_FLAGS = frozenset({"-g2005-sv", "-g2009", "-g2012"})


def _icarus_sv_flag_state(
    name: str,
    ref: fusesoc_registry.TargetRef,
    sources: fusesoc_registry.CoreSources,
) -> bool | None:
    """Return SV-flag presence, or ``None`` when the check does not apply."""
    has_sv = any(
        str(path).lower().endswith(".sv")
        for path in (*sources.rtl_source_files, *sources.tb_files)
    )
    if not has_sv:
        return None
    try:
        doc = fusesoc_registry.read_core(ref.core_file)
    except fusesoc_registry.FuseSocError:
        return None
    options = fusesoc_registry.core_target_flow_option(doc, name, "iverilog_options")
    flags = {str(option) for option in options} if isinstance(options, list) else set()
    return bool(flags & _ICARUS_SV_FLAGS)


def _report_missing_icarus_sv_flag(
    name: str,
    ref: fusesoc_registry.TargetRef,
    selected_targets: set[str] | None,
    _note: Check,
    _warn: Check,
    _fail: Fail,
) -> None:
    """Report a missing Icarus SV flag at Target-appropriate severity."""
    message = (
        f"Target '{name}': SV sources with Icarus but iverilog_options "
        "missing -g2012 — iverilog defaults to Verilog-2005 and rejects "
        "SystemVerilog syntax (`logic`, `always_ff`) at compile"
    )
    fix = f"add iverilog_options: [-g2012] to the Target's flow_options in {ref.core_file.name}"
    qualified = f"{ref.vlnv}#{name}"
    selected = (
        selected_targets is None or name in selected_targets or qualified in selected_targets
    )
    if not selected:
        _note(
            f"{message} — this Target is not selected by the project, so modernizing it is optional"
        )
    elif ref.flow == "sim":
        _fail(message, fix)
    else:
        _warning_sink(_warn, "core.icarus-sv-mode-missing", subject=name)(f"{message}; fix: {fix}")


def _check_icarus_sv_language_mode(
    root: Path,
    refs: dict,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
    selected_targets: set[str] | None = None,
    _inputs: _CoreAuditInputs | None = None,
) -> None:
    """Flag Icarus Targets whose SystemVerilog sources lack an SV flag."""
    icarus_targets = [
        (name, ref)
        for name, ref in sorted(refs.items())
        if (ref.eda_tool or "").lower() == "icarus"
    ]
    if not icarus_targets:
        return
    inputs = _inputs or _CoreAuditInputs(root, refs)
    for name, ref in icarus_targets:
        try:
            sources = inputs.sources_for(name)
        except fusesoc_registry.FuseSocError:
            continue  # an unresolvable Target is enumeration's to report
        flag_state = _icarus_sv_flag_state(name, ref, sources)
        if flag_state is None:
            continue
        if flag_state:
            _pass(f"Target '{name}' (icarus) declares an SV language flag for its .sv sources")
            continue
        _report_missing_icarus_sv_flag(
            name,
            ref,
            selected_targets,
            _note or _pass,
            _warn,
            _fail,
        )


# Verilator's own build-a-binary entry points: both generate the vanilla auto
# main (`traceEverOn(true)` but no tracer object), so `simulate --trace` has
# nothing to hook. A trace-capable Verilator sim Target instead ships a cppSource
# `--exe` main that opens the VerilatedVcdC on `+trace` — so the presence of
# either flag is the definitive "cannot trace via the overlay" signal.
_VERILATOR_AUTO_MAIN_FLAGS = frozenset({"--main", "--binary"})


def _check_sim_traceable(
    root: Path,
    refs: dict,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when a Verilator sim Target cannot produce a waveform under --trace.

    Booley traces a Verilator Target by injecting `--trace` through a generated
    overlay ``.core`` and passing ``+trace +tracefile=`` at run time
    (:mod:`booley.fusesoc.fusesoc_trace_overlay`) — it never *synthesises* a tracer. A
    Target built with Verilator's auto ``--main`` (or ``--binary``) has no C++
    main to construct a ``VerilatedVcdC``, so ``--trace`` hooks into nothing: the
    run PASSES but the store is a bare ~443-byte FST header with **0 signals** —
    a silent trap that otherwise surfaces only on the first trace run. The
    remedy is the convention traceable sim Targets already follow: a committed
    ``cppSource`` ``--exe`` main that opens the tracer on ``+trace``, and drop
    ``--main``.

    Verilator-only by design: for Icarus/Xcelium/VCS the trace overlay
    *auto-supplies* the ``booley_vcd_dump`` module from Booley's ``refs/`` when
    the design lacks it (:func:`booley.fusesoc.fusesoc_trace_overlay._inject_dump_module`),
    so those EDA tools self-heal — a pre-flight check earns nothing there. The
    findings are aggregated into a single WARN: a project with many untraced unit
    TBs (each independently fixable) should not spray one WARN per Target and
    train the reader to ignore the tier.
    """
    verilator_sims = [
        (name, ref)
        for name, ref in sorted(refs.items())
        if ref.flow == "sim" and (ref.eda_tool or "").lower() == "verilator"
    ]
    if not verilator_sims:
        return
    non_traceable: list[str] = []
    traceable = 0
    for name, ref in verilator_sims:
        try:
            doc = fusesoc_registry.read_core(ref.core_file)
        except fusesoc_registry.FuseSocError:
            continue  # a malformed .core is the .core-schema check's to report
        opts = fusesoc_registry.core_target_flow_option(doc, name, "verilator_options") or []
        if {str(o) for o in opts} & _VERILATOR_AUTO_MAIN_FLAGS:
            non_traceable.append(name)
        else:
            traceable += 1
    if traceable:
        _pass(f"{traceable} Verilator sim Target(s) trace-capable (own a --exe main)")
    if non_traceable:
        offending = ", ".join(sorted(non_traceable))
        _warning_sink(_warn, "sim.trace-unavailable", subject=offending)(
            f"Verilator sim Target(s) cannot trace: {offending} — they build with "
            "Verilator's auto --main/--binary, which has no tracer for "
            "`sim --trace` to hook, so a trace run PASSES with an empty "
            "0-signal waveform store and no error. Give each a cppSource --exe "
            "main that opens VerilatedVcdC on +trace and drop --main from "
            "verilator_options (mirror a traceable sim Target's tb_cpp fileset)."
        )


# A SystemVerilog interface port on a module's port list, e.g.
#     taxi_axis_if.snk  s_axis_tx,
# The `<type>.<modport> <name>` shape is unambiguous — no other port syntax has
# a dot in the type position — so matching it is safe. A *bare* interface port
# (`taxi_axis_if s_axis_tx`) is deliberately NOT matched: it is textually
# identical to an ordinary user-defined-type port (`my_pkg_t cfg`), and a check
# that cried wolf on every typed port would be worse than no check.
_IFACE_PORT_RE = re.compile(
    r"^\s*(?P<iface>[A-Za-z_]\w*)\.(?P<modport>[A-Za-z_]\w*)\s+(?P<port>[A-Za-z_]\w*)\s*(?:,|\)|$)",
    re.MULTILINE,
)

# Line comments are stripped before the header scan so a commented-out example
# port (or a `// foo.bar baz` note) cannot fabricate a finding.
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _module_header(text: str, module: str) -> str:
    """The ``module <module> ... ;`` header (params + port list), or ``""``.

    Everything from the module keyword to the first ``;`` — which, for a module
    declaration, is exactly the end of the ANSI port list.
    """
    stripped = _LINE_COMMENT_RE.sub("", text)
    m = re.search(rf"\bmodule\s+{re.escape(module)}\b", stripped)
    if not m:
        return ""
    end = stripped.find(";", m.end())
    return stripped[m.start() : end if end != -1 else len(stripped)]


def _interface_ports(
    root: Path,
    sources: fusesoc_registry.CoreSources,
    toplevel: str,
) -> list[str] | None:
    """Interface ports on *toplevel*, or ``None`` if its source can't be read.

    ``None`` (source not found / unreadable) and ``[]`` (found, no interface
    ports) are deliberately distinct: only the latter is worth a PASS.
    """
    for rel in sources.rtl_source_files:
        path = root / rel
        if path.suffix.lower() not in _HDL_SUFFIXES:
            continue
        try:
            header = _module_header(path.read_text(encoding="utf-8", errors="replace"), toplevel)
        except OSError:
            continue
        if not header:
            continue  # this file doesn't declare the toplevel
        return [
            f"{m.group('iface')}.{m.group('modport')} {m.group('port')}"
            for m in _IFACE_PORT_RE.finditer(header)
        ]
    return None


def _target_interface_ports(
    root: Path,
    inputs: _CoreAuditInputs,
    name: str,
    ref: fusesoc_registry.TargetRef,
) -> tuple[str, list[str]] | None:
    """Return an auditable Target's toplevel and interface ports."""
    try:
        doc = fusesoc_registry.read_core(ref.core_file)
    except fusesoc_registry.FuseSocError:
        return None
    toplevel = fusesoc_registry.core_target_toplevel(doc, name)
    if not toplevel:
        return None
    try:
        sources = inputs.sources_for(name)
    except (fusesoc_registry.FuseSocError, OSError):
        return None
    ports = _interface_ports(root, sources, toplevel)
    return None if ports is None else (toplevel, ports)


def _check_toplevel_interface_ports(
    root: Path,
    refs: dict,
    _pass: Check,
    _warn: Check,
    *,
    _inputs: _CoreAuditInputs | None = None,
) -> None:
    """Warn when a lint/synth toplevel cannot elaborate without interfaces."""
    inputs = _inputs or _CoreAuditInputs(root, refs)
    for name, ref in sorted(refs.items()):
        if ref.flow == "sim":
            continue
        target_ports = _target_interface_ports(root, inputs, name, ref)
        if target_ports is None:
            continue
        toplevel, ports = target_ports
        if not ports:
            _pass(f"{ref.flow} Target '{name}' toplevel '{toplevel}' elaborates standalone")
            continue
        shown = ", ".join(ports[:3]) + (f", +{len(ports) - 3} more" if len(ports) > 3 else "")
        _warning_sink(_warn, "core.interface-toplevel", subject=name)(
            f"{ref.flow} Target '{name}' toplevel '{toplevel}' has SystemVerilog "
            f"interface ports ({shown}) — it cannot be elaborated standalone, so "
            f"{ref.flow} will fail on an interface parameter mismatch, not on a "
            "real defect in the RTL",
            "point this Target at a flat-port wrapper: a thin module that "
            "instantiates the interfaces with real parameters, instantiates the "
            "toplevel, and exposes plain signal ports of its own",
        )


# A committed compiled artifact (firmware image, memory init) whose *source*
# lives in the same directory is a "ship the toolchain, not the artifact"
# violation (booley-setup SKILL.md): it should be rebuilt on demand inside the
# sandbox, not frozen into git. Suffixes that read as build outputs, and the
# sibling-source signals that mark them as buildable-in-repo.
_BUILD_ARTIFACT_SUFFIXES = frozenset({".hex", ".mem", ".bin", ".elf"})
_BUILD_SOURCE_SUFFIXES = frozenset({".c", ".s", ".cc", ".cpp", ".cxx", ".asm", ".rs"})
_BUILD_SOURCE_NAMES = frozenset({"makefile", "cmakelists.txt"})


def _looks_built_from_sibling_source(path: Path) -> bool:
    """True when ``path``'s directory also holds compiler/assembler source.

    Case-folded so ``start.S`` (assembly) and a ``Makefile`` both count. A
    firmware ``.hex`` next to ``hello.c`` / ``start.S`` / a ``Makefile`` is a
    build output of in-repo source, not an opaque vendored blob.
    """
    parent = path.parent
    if not parent.is_dir():
        return False
    for entry in parent.iterdir():
        if not entry.is_file():
            continue
        name = entry.name.lower()
        if name in _BUILD_SOURCE_NAMES or entry.suffix.lower() in _BUILD_SOURCE_SUFFIXES:
            return True
    return False


def _check_committed_build_artifacts(
    root: Path,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when a compiled artifact built from in-repo source is committed.

    A vendored ``firmware.hex`` sitting next to its ``*.c`` / ``*.S`` / Makefile
    is a frozen build output: it hides the real dependency (the cross-compiler)
    and rots on the first source change. The supported fix is to bake the
    toolchain into the project sandbox image and rebuild on demand from a
    post-setup hook (booley-setup SKILL.md: "ship the toolchain, not the
    artifact"). Warn, not fail — the committed blob still simulates locally.
    An opaque vendored blob with no sibling source is left alone (may be the
    only option); the untracked-data trap is handled by
    :func:`_check_core_files_tracked`. A file the ``.core`` explicitly tags
    ``tags: [vendored]`` is exempt too — that is the maintainer asserting the
    blob is upstream-shipped with no rebuildable in-repo source, which the
    sibling-source heuristic cannot otherwise tell from a frozen local build.
    (``tags`` is the CAPI2-valid marker; a bare ``vendored: true`` key breaks
    real fusesoc and is rejected by the shallow ``.core`` check — see QA-3.)
    """
    referenced = fusesoc_registry.all_referenced_files(root)
    vendored = fusesoc_registry.vendored_files(root)
    suspects = [
        rel
        for rel in referenced
        if Path(rel).suffix.lower() in _BUILD_ARTIFACT_SUFFIXES
        and rel not in vendored
        and (root / rel).is_file()
        and _looks_built_from_sibling_source(root / rel)
    ]
    if not suspects:
        return  # nothing that looks built-from-source; stay quiet (no _pass noise)
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", *suspects],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return  # not a git repo / git missing — tracking is unknowable, stay quiet
    if result.returncode != 0:
        return
    tracked = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    committed = [rel for rel in suspects if rel.replace("\\", "/") in tracked]
    if committed:
        shown = ", ".join(committed[:5]) + (", …" if len(committed) > 5 else "")
        _warning_sink(
            _warn,
            "project.committed-build-artifact",
            subject=",".join(sorted(committed)),
        )(
            f"{len(committed)} committed artifact(s) look built from in-repo "
            f"source ({shown}) — ship the toolchain in the sandbox image and "
            "rebuild on demand (project Dockerfile + post-setup hook) instead of "
            "committing a frozen blob (booley-setup SKILL.md)"
        )


def _check_core_files_tracked(
    root: Path,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when a ``.core`` references a file that exists on disk but is untracked.

    The silent vendored-data trap: a data file (firmware ``.hex``, memory-init
    image) matching the upstream ``.gitignore`` gets ``git add``-ed to no effect,
    so it stays untracked. On disk it is present (doctor sees it), but a fresh
    clone / CI checkout would lack it and the testbench would fail opaquely. A
    warn with the ``git add -f`` remedy, not a fail: the local run still works.

    State-zone files (ADR 0036 stealth cores) are judged against the project
    dir's *own* git repo — ``.booley_project/`` is invisible to the host repo by
    design, so asking the host is guaranteed noise. No repo there → skip them
    (nothing knowable). Tracking is symlink-aware in both zones: a path whose
    ancestor is a tracked symlink (a stealth core's reach-through like
    ``cores/dhrystone -> ../../dhrystone``) arrives with a fresh clone, so it
    counts as tracked. It is submodule-aware for the same reason — see
    ``_tracked_set``.
    """
    referenced = fusesoc_registry.all_referenced_files(root)
    on_disk = [rel for rel in referenced if (root / rel).is_file()]
    if not on_disk:
        return
    state_prefix = fusesoc_registry.state_cores_dir(root).parent.name  # ".booley_project"
    host_files = [
        rel for rel in on_disk if not rel.replace("\\", "/").startswith(state_prefix + "/")
    ]
    state_files = [rel for rel in on_disk if rel.replace("\\", "/").startswith(state_prefix + "/")]

    def _tracked_set(cwd: Path) -> set[str] | None:
        """Tracked paths under *cwd*, submodule contents included.

        Plain ``git ls-files`` stops at a submodule boundary and lists only the
        gitlink, so every file *inside* a vendored submodule read as untracked —
        a false alarm, since the submodule's own repo tracks them and
        `git submodule update` restores them on a fresh clone.
        ``--recurse-submodules`` descends and reports each file at its
        superproject-relative path, which is the vocabulary the caller compares
        against. A git too old for the flag retries plain rather than go silent.
        """
        for cmd in (["git", "ls-files", "--recurse-submodules"], ["git", "ls-files"]):
            try:
                result = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (FileNotFoundError, subprocess.SubprocessError):
                return None  # git missing — tracking is unknowable, stay quiet
            if result.returncode == 0:
                return {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return None  # not a git repo

    def _is_tracked(rel: str, tracked: set[str]) -> bool:
        # git ls-files reports repo-relative forward-slash paths. An ancestor
        # entry in the tracked set can only be a tracked symlink (git never
        # lists directories) — reach-through content still ships with a clone.
        norm = rel.replace("\\", "/")
        if norm in tracked:
            return True
        parts = norm.split("/")
        return any("/".join(parts[:i]) in tracked for i in range(1, len(parts)))

    untracked: list[str] = []
    host_tracked = _tracked_set(root)
    if host_tracked is not None:
        untracked += [rel for rel in host_files if not _is_tracked(rel, host_tracked)]
    state_dir = root / state_prefix
    if state_files and (state_dir / ".git").exists():
        state_tracked = _tracked_set(state_dir)
        if state_tracked is not None:
            untracked += [
                rel
                for rel in state_files
                if not _is_tracked(str(PurePosixPath(*Path(rel).parts[1:])), state_tracked)
            ]
    if untracked:
        shown = ", ".join(untracked[:5]) + (", …" if len(untracked) > 5 else "")
        _warning_sink(
            _warn,
            "project.core-file-untracked",
            subject=",".join(sorted(untracked)),
        )(
            f"{len(untracked)} .core-referenced file(s) exist on disk but are "
            f"untracked by git ({shown}) — if gitignored (vendored firmware / "
            "build artifact), force-add with `git add -f <file>` and verify with "
            "`git ls-files <file>`; a fresh clone or CI checkout will lack them"
        )
    else:
        _pass(f"all {len(on_disk)} .core-referenced on-disk file(s) tracked by git")


# The $readmemh/$readmemb memory-load call pattern (HDL suffixes: _HDL_SUFFIXES).
_READMEM_RE = re.compile(r"\$readmem[hb]\s*\(\s*\"([^\"]+)\"")


def _readmem_literal_targets(root: Path) -> list[str]:
    """Project-relative memory images named by a *literal* $readmemh/$readmemb.

    Scans the ``.core``-referenced HDL for ``$readmem[hb]("path")`` calls and
    resolves each literal against the repo root and the calling file's own
    directory. Only plain literals are considered; a path built from a plusarg,
    a format specifier, or concatenation can't be resolved statically and is
    skipped. Used to catch memory images the testbench hardcodes but that no
    ``.core`` fileset lists — the gap :func:`_check_core_files_tracked` misses.
    """
    targets: set[str] = set()
    root_res = root.resolve()
    for rel in fusesoc_registry.all_referenced_files(root):
        if Path(rel).suffix.lower() not in _HDL_SUFFIXES:
            continue
        src = root / rel
        if not src.is_file():
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _READMEM_RE.finditer(text):
            literal = match.group(1)
            if any(ch in literal for ch in "%{}+*?"):
                continue  # constructed / format / plusarg / glob — not a static path
            for base in (root, src.parent):
                cand = base / literal
                if cand.is_file():
                    with contextlib.suppress(ValueError):
                        targets.add(cand.resolve().relative_to(root_res).as_posix())
                    break
    return sorted(targets)


def _check_readmemh_targets_tracked(
    root: Path,
    _pass: Check,
    _warn: Check,
) -> None:
    """Warn when a hardcoded $readmemh memory image exists on disk but is untracked.

    A testbench that boots from ``$readmemh("firmware.vmem", mem)`` depends on a
    file no ``.core`` fileset lists, so :func:`_check_core_files_tracked` can't
    see it. If that image matches a blanket ``.gitignore`` (vendored firmware,
    build output), ``git add`` no-ops and a fresh clone / CI checkout lacks it —
    the sim then spins on uninitialized RAM (SETUP-12, and the static companion
    to the runtime readmemh-missing guard). Warn with the ``git add -f`` remedy.
    """
    targets = _readmem_literal_targets(root)
    if not targets:
        return
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", *targets],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return  # not a git repo / git missing — tracking is unknowable, stay quiet
    if result.returncode != 0:
        return
    tracked = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    untracked = [rel for rel in targets if rel.replace("\\", "/") not in tracked]
    if untracked:
        shown = ", ".join(untracked[:5]) + (", …" if len(untracked) > 5 else "")
        _warning_sink(
            _warn,
            "project.memory-image-untracked",
            subject=",".join(sorted(untracked)),
        )(
            f"{len(untracked)} $readmemh/$readmemb memory image(s) exist on disk "
            f"but are untracked by git ({shown}) — if gitignored (vendored "
            "firmware / build output), force-add with `git add -f <file>`; a "
            "fresh clone or CI checkout will lack them and the sim boots "
            "uninitialized RAM"
        )
    else:
        _pass(f"all {len(targets)} $readmemh-referenced image(s) tracked by git")


def _project_write_scope(root: Path) -> list[str]:
    """The agent's project-wide writable Scope as glob patterns (category dirs).

    doctor has no ticket Scope, so the ``.core`` security audit treats the union
    of the writable category dirs (``rtl/``, ``tb/``, …) as the agent's Scope: an
    imperative ``.core`` script under one of them is agent-mutable, hence not
    confined (decision 21). A script under, say, ``scripts/`` is out-of-Scope and
    passes.
    """
    dirs: set[str] = set()
    try:
        category_dirs = get_category_dirs(root)
    except fusesoc_registry.FuseSocError:
        # No .core at all — the core audit already hard-FAILs that (ADR
        # 0039); don't crash the audit deriving a Scope from nothing.
        return []
    for prefixes in category_dirs.values():
        for prefix in prefixes:
            if prefix.endswith(("/", "\\")):
                stem = prefix.rstrip("/\\")
                dirs.add(f"{stem}/*")
            else:
                dirs.add(prefix)
    return sorted(dirs)


def _project_has_cocotb_target(project: ProjectAudit | None) -> bool:
    """True when any selectable Target declares a ``cocotb_module`` (ADR 0034).

    Cheap ``.core`` YAML read; gates the conditional cocotb doctor checks so a
    plain-SV project is never faulted for a capability it doesn't use. The
    container checks also run without a project audit (``project is None``) —
    no project, no Cocotb Target.
    """
    if project is None:
        return False
    try:
        refs = fusesoc_registry.enumerate_targets(project.project_root)
    except fusesoc_registry.FuseSocError:
        return False
    return any(ref.cocotb_module for ref in refs.values())


def _load_tests_toml_normalized(project: ProjectAudit) -> dict[str, dict]:
    """The normalized tests.toml sections, or empty when absent/invalid.

    Invalidity is _audit_tests_toml's finding, not this reader's — the cocotb
    checks just degrade to the no-tests view.
    """
    tests_path = project.project_dir / "tests.toml"
    if not tests_path.exists():
        return {}
    try:
        with tests_path.open("rb") as f:
            return normalize_tests_toml(tomllib.load(f))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}


def _cocotb_module_file(
    ref: fusesoc_registry.TargetRef,
    module: str,
) -> tuple[Path, bool] | None:
    """Locate the Target's cocotb module file among its filesets.

    Matches a fileset entry whose ``copyto`` destination or source basename is
    ``<module>.py``. Returns ``(source path, fileset tb-tagged)`` or ``None``.
    """
    try:
        doc = fusesoc_registry.read_core(ref.core_file)
    except fusesoc_registry.FuseSocError:
        return None
    targets = doc.get("targets") or {}
    target_def = targets.get(ref.name) if isinstance(targets, dict) else None
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
                return ref.core_file.parent / str(path), tagged
    return None


# A cocotb module that registers its tests through a factory: the names land in
# the module namespace at import time (`TestFactory(...).generate_tests()` emits
# run_test_001…), so they are invisible to a `def <name>(` grep. Recognizing the
# call is what lets doctor say "not statically verifiable" instead of reporting
# every generated name as missing.
_COCOTB_FACTORY_RE = re.compile(r"\b(?:TestFactory|generate_tests)\b")


def _check_cocotb_targets(
    project: ProjectAudit,
    refs: dict[str, fusesoc_registry.TargetRef],
    _pass: Check,
    _warn: Fail,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Conditional Cocotb Target checks (ADR 0034 / F1).

    Fires only when a Target declares ``flow_options.cocotb_module``:

    * a tests.toml plusarg ``select`` template on it is a config
      contradiction (selection is the ``COCOTB_TEST_FILTER`` env var) — FAIL;
    * the module file should exist in the Target's filesets and its fileset
      be tagged ``tb`` (Source Isolation) — advisory WARN;
    * declared test names should appear as functions in the module file
      (grep-level; the authoritative check stays reactive via results.xml,
      decision 7) — advisory WARN, but SKIPped when the module builds its
      tests with a factory (see :data:`_COCOTB_FACTORY_RE`): a
      ``TestFactory``-generated name exists only at import time, so a
      `def <name>(` grep cannot see it and every such name would read as
      missing (14/14 on taxi). Not verifiable statically ⇒ report it as such
      rather than as a finding.
    """
    cocotb_refs = {n: r for n, r in refs.items() if r.cocotb_module}
    if not cocotb_refs:
        return
    sections = _load_tests_toml_normalized(project)
    for name, ref in sorted(cocotb_refs.items()):
        module = ref.cocotb_module or ""
        section = sections.get(name, {})
        if "select" in section:
            _fail(
                f"cocotb Target '{name}' declares a tests.toml `select` plusarg template",
                "remove `select` — cocotb test selection is the "
                "COCOTB_TEST_FILTER env var built from the `tests` list "
                "(ADR 0034)",
            )
        found = _cocotb_module_file(ref, module)
        if found is None:
            _warning_sink(_warn, "cocotb.module-file-missing", subject=name)(
                f"cocotb Target '{name}': no fileset file matches its "
                f"cocotb_module '{module}' ({module}.py)",
                f"add the Python testbench to a tagged fileset with copyto: {module}.py",
            )
            continue
        source, tagged = found
        if not tagged:
            _warning_sink(_warn, "cocotb.tb-tag-missing", subject=name)(
                f"cocotb Target '{name}': the fileset carrying {module}.py "
                "is not tagged tags:[tb]",
                "tag it so Source Isolation partitions RTL vs TB "
                "(ADR 0022 dec 13 / ADR 0034 dec 4)",
            )
        if not source.is_file():
            _warning_sink(_warn, "cocotb.module-file-missing", subject=name)(
                f"cocotb Target '{name}': module file {source} not found on disk",
                "author the Python testbench (or fix the fileset path)",
            )
            continue
        _pass(f"cocotb Target '{name}' module file present ({module}.py)")
        declared = section.get("tests") or []
        if declared:
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            missing = [t for t in declared if not re.search(rf"def\s+{re.escape(t)}\s*\(", text)]
            if missing and _COCOTB_FACTORY_RE.search(text):
                # Factory-generated names are registered at import time, not
                # authored as `def <name>(` — the grep is structurally blind to
                # them. Say the check cannot run; do not fault the project.
                _skip(
                    f"cocotb Target '{name}': {len(missing)} tests.toml name(s) "
                    f"cannot be verified statically — {module}.py generates its "
                    "tests with a factory (TestFactory/generate_tests); the names "
                    "are checked against results.xml at run time (ADR 0034 dec 7)"
                )
            elif missing:
                _warning_sink(_warn, "cocotb.test-name-missing", subject=name)(
                    f"cocotb Target '{name}': tests.toml names not found as "
                    f"functions in {module}.py: {', '.join(missing)} "
                    "(grep-level, advisory)",
                    "check the names against the @cocotb.test functions — a "
                    "mismatch surfaces at run time as inconclusive",
                )
            else:
                _pass(
                    f"cocotb Target '{name}' tests.toml names all present in {module}.py",
                )


#: Native headers whose development package the base sandbox does NOT carry,
#: mapped to the Debian package that provides them. Requirements baking is
#: Python-only, so a DPI/testbench C++ file that includes one of these compiles
#: nowhere until the project image installs it — Ibex's `dpi_memutil.cc`
#: includes <libelf.h> and its Verilator target links -lelf, and the derived
#: image shipped only the runtime `libelf.so.1` (F-9). Keep this list to
#: headers genuinely absent from the base image; a false warning here costs
#: more trust than the missing hint it replaces.
_NATIVE_HEADER_PACKAGES = {
    "libelf.h": "libelf-dev",
    "gelf.h": "libelf-dev",
    "elfutils/libdw.h": "libdw-dev",
    "png.h": "libpng-dev",
    "jpeglib.h": "libjpeg-dev",
    "curl/curl.h": "libcurl4-openssl-dev",
    "ssl/ssl.h": "libssl-dev",
    "openssl/ssl.h": "libssl-dev",
    "xml2/libxml/parser.h": "libxml2-dev",
    "libxml/parser.h": "libxml2-dev",
    "sqlite3.h": "libsqlite3-dev",
    "boost/version.hpp": "libboost-dev",
    "gmp.h": "libgmp-dev",
    "mpfr.h": "libmpfr-dev",
    "ncurses.h": "libncurses-dev",
    "systemc.h": "libsystemc-dev",
}

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)

_C_FAMILY_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})

#: Cap on files read for the include scan. The check is an advisory hint, not
#: an audit; a monorepo with thousands of C++ files must not make doctor slow.
_NATIVE_SCAN_FILE_CAP = 400


def _dockerfile_declares_native_package(dockerfile: Path, package: str) -> bool:
    """Return whether an apt install command in *dockerfile* names *package*.

    Project-owned images are the documented remedy for native headers. Doctor
    runs inside the Session Runtime too, where Docker is deliberately absent,
    so the hand-authored Dockerfile is the portable evidence available on both
    venues. Ignore comments and require the package to occur as an install
    argument, not merely elsewhere in the file.
    """
    try:
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    uncommented = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    joined = re.sub(r"\\\s*\n", " ", uncommented)
    for match in re.finditer(r"(?:apt-get|apt)\s+install\b([^;&\n]*)", joined):
        if package in re.findall(r"[A-Za-z0-9.+:-]+", match.group(1)):
            return True
    return False


def _report_native_dependencies(
    project: ProjectAudit,
    needed: dict[str, set[str]],
    _pass: Check,
    _warn: Check,
) -> None:
    """Report curated native packages, honoring the project image recipe."""
    dockerfile = project.project_dir / "docker" / "Dockerfile"
    declared = {
        package for package in needed if _dockerfile_declares_native_package(dockerfile, package)
    }
    if declared:
        _pass(
            "native build dependencies declared in project Dockerfile: "
            + ", ".join(sorted(declared))
        )
    for package, users in sorted(needed.items()):
        if package in declared:
            continue
        where = ", ".join(sorted(users)[:3])
        more = f" (+{len(users) - 3} more)" if len(users) > 3 else ""
        _warning_sink(_warn, "sandbox.native-dependency-missing", subject=package)(
            f"C/C++ sources need the native package '{package}' ({where}{more}), "
            "which the base sandbox image does not carry. Baking Python "
            "requirements cannot pull in native deps — add it to "
            ".booley_project/docker/Dockerfile and rebuild, or the simulation "
            "build will fail on a missing header/library"
        )


def _audit_native_dependencies(project: ProjectAudit, _pass: Check, _warn: Check) -> None:
    """Warn when a Target's C/C++ sources need a native package the image lacks.

    ``booley init`` bakes the listed Python requirements only, so a native
    build dependency is invisible to it: the project image is built, the flow
    resolves, and the failure surfaces as a compile/link error deep inside a
    simulation build. Naming the package at setup time is much cheaper than
    debugging a missing header later.

    Advisory by design — this is a curated header list, not a resolver, so it
    can only be a hint. It never fails the gate.
    """
    seeds = _project_target_matrix(project).seed_targets
    if not seeds:
        return
    root = project.project_root
    sources: list[str] = []
    for token in seeds:
        try:
            sources.extend(
                fusesoc_registry.target_source_files(
                    root, token, include_dependencies=True
                ).rtl_source_files
            )
        except fusesoc_registry.FuseSocError:
            continue  # an unresolvable Target is already reported elsewhere

    needed: dict[str, set[str]] = {}
    scanned = 0
    for rel in dict.fromkeys(sources):
        if Path(rel).suffix.lower() not in _C_FAMILY_SUFFIXES:
            continue
        if scanned >= _NATIVE_SCAN_FILE_CAP:
            break
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for header in _INCLUDE_RE.findall(text):
            package = _NATIVE_HEADER_PACKAGES.get(header)
            if package:
                needed.setdefault(package, set()).add(rel)

    if not scanned:
        return  # no C/C++ in the selected Targets — nothing this check applies to
    if not needed:
        _pass(f"no known-missing native build dependencies in {scanned} C/C++ source(s)")
        return
    _report_native_dependencies(project, needed, _pass, _warn)


def _audit_tests_toml(project: ProjectAudit, _pass: Check, _skip: Check, _fail: Fail) -> None:
    """Validate the project's ``tests.toml`` selector templates."""
    tests_path = project.project_dir / "tests.toml"
    if not tests_path.exists():
        _skip("tests.toml not present (test selection falls back to configs.toml)")
        return
    try:
        with tests_path.open("rb") as f:
            raw = tomllib.load(f)
        sections = normalize_tests_toml(raw)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        _fail(f"tests.toml invalid: {exc}", "fix the tests.toml select/test list")
        return
    _pass("tests.toml valid (single-token select templates well-formed)")
    _audit_tests_toml_targets(project, sections, _fail)


def _audit_tests_toml_targets(project: ProjectAudit, sections: dict, _fail: Fail) -> None:
    """Check every tests.toml section names a Target that actually exists.

    A section keyed to a Target no one declares is dead config that looks
    live: the lookup misses, no selector reaches the binary, and the
    testbench runs its *default* test to a green PASS. That is the same
    silent-uninitialized-run failure the qualifier-tolerant lookup fixes, so
    validation has to agree with execution about which keys resolve.
    """
    try:
        refs = fusesoc_registry.enumerate_targets(project.project_root)
    except fusesoc_registry.FuseSocError:
        return  # enumeration failure is already reported by the structural audit
    if not refs:
        return
    known = set(refs) | {f"{ref.vlnv}#{name}" for name, ref in refs.items()}
    bare_known = {name.rsplit("#", 1)[-1] for name in known}
    for key in sections:
        if key in known or key.rsplit("#", 1)[-1] in bare_known:
            continue
        _fail(
            f"tests.toml section [{key}] names no declared .core Target",
            "fix the section key (a dead key runs the testbench's default "
            "test and reports a false PASS)",
        )


def _report_core_resolve(
    name: str,
    ok: bool,
    err: str,
    selected: bool,
    _pass: Check,
    _warn: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
) -> None:
    """Grade one Target resolution by whether Doctor explicitly selected it.

    A vendored monorepo carries Targets Booley was never pointed at — ibex has
    208 cores, whose formal/vendor Targets need EDA tools this project does not
    configure. Failing the setup gate on those makes a green doctor
    unreachable for reasons the port cannot fix, and contradicts plain doctor,
    which already calls the same Targets harmless advisories. Only the
    Doctor Targets are a gate; their dependency closure is covered
    transitively, since a broken dependency fails the Target that needs it.
    """
    if ok:
        _pass(f".core Target '{name}' resolves")
        return
    if selected:
        _fail(f".core Target '{name}' fails to resolve", "fix the .core / depends graph")
        _print_text_excerpt(err)
        return
    (_note or _pass)(
        f".core Target '{name}' fails to resolve, but Doctor does not "
        "select it - advisory only (unselected vendored/upstream Target)"
    )


def _run_core_resolve_checks(
    project: ProjectAudit,
    docker_exe: str | None,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
) -> None:
    """Deep: resolve each ``.core`` Target through ``fusesoc`` (subprocess).

    Resolution runs Edalize's ``configure()``, which needs the ``fusesoc``
    console script + toolchain — that environment IS the Sandbox image, so we
    resolve INSIDE the container (mirroring the deep Flow checks). Only when the
    image is unavailable do we fall back to a host ``fusesoc`` if one happens to
    be installed; failing that, the check skips rather than failing.

    Only Targets selected by ``flow_options.booley.doctor`` gate the run; see
    ``_report_core_resolve``.
    """
    # Enumerating Targets is a pure ``.core`` YAML read (no fusesoc needed) and
    # cheap, so do it host-side to decide whether there is anything to resolve.
    try:
        refs = fusesoc_registry.enumerate_targets(project.project_root)
    except fusesoc_registry.FuseSocError:
        return  # the structural audit already reported the enumeration failure
    if not refs:
        return

    is_configured = _project_target_matrix(project).is_selected

    image = _sandbox_image(project)
    if docker_exe and _docker_image_exists_by_name(image):
        _run_core_resolve_in_docker(
            project,
            docker_exe,
            image,
            refs,
            is_configured,
            _pass,
            _warn,
            _fail,
            _note=_note,
        )
        return

    if shutil.which("fusesoc") is None:
        _skip(
            ".core resolvability skipped - Sandbox image unavailable and "
            "fusesoc not on host PATH (build the image with 'booley init')"
        )
        return
    for name, ref in sorted(refs.items()):
        build_root = project.project_root / ".booley_project" / ".runtime" / "doctor" / name
        try:
            fusesoc_registry.resolve_target(
                name,
                project_root=project.project_root,
                build_root=build_root,
                vlnv=ref.vlnv,
            )
            _report_core_resolve(name, True, "", True, _pass, _warn, _fail, _note=_note)
        except fusesoc_registry.FuseSocError as exc:
            _report_core_resolve(
                name,
                False,
                str(exc),
                is_configured(name, ref.vlnv),
                _pass,
                _warn,
                _fail,
                _note=_note,
            )


# Container-side resolver: enumerate + resolve every Target in one docker run and
# emit a machine-readable JSON verdict line the host parses back into checks.
# Kept as a module constant so the quoting is auditable in one place.
_CORE_RESOLVE_SNIPPET = (
    "import json\n"
    "from booley.fusesoc import fusesoc_registry as fr\n"
    "root = '/work'\n"
    "refs = fr.enumerate_targets(root)\n"
    "out = []\n"
    "for name, ref in sorted(refs.items()):\n"
    "    br = '/work/.booley_project/.runtime/doctor/' + name\n"
    "    try:\n"
    "        fr.resolve_target(name, project_root=root, build_root=br, vlnv=ref.vlnv)\n"
    "        out.append({'name': name, 'ok': True})\n"
    "    except fr.FuseSocError as exc:\n"
    "        out.append({'name': name, 'ok': False, 'err': str(exc)})\n"
    "print('[[CORE_RESOLVE_JSON]]' + json.dumps(out))\n"
)


def _run_core_resolve_in_docker(
    project: ProjectAudit,
    docker_exe: str,
    image: str,
    refs: dict,
    is_configured: Callable[[str, str], bool],
    _pass: Check,
    _warn: Check,
    _fail: Fail,
    *,
    _note: Check | None = None,
) -> None:
    """Resolve every ``.core`` Target inside the Sandbox image, one docker run."""
    inner = ["python3", "-c", _CORE_RESOLVE_SNIPPET]
    cmd = _docker_wrap(docker_exe, image, project.project_root, inner)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _fail(
            ".core resolvability timed out after 600s in sandbox",
            "check the .core / depends graph for a resolution hang",
        )
        return
    except OSError as exc:
        _fail(f".core resolvability failed to start container: {exc}", "check docker")
        return

    if _sandbox_guard_failed(proc):
        # The in-container self-assertion refused: the resolve snippet was
        # composed for the sandbox but executed outside it (doctor misrouting,
        # b5e8681's failure class). Loud FAIL — never let this look like a mere
        # "no verdict" or resolve failure.
        _fail(
            ".core resolvability was routed into the sandbox but executed "
            "OUTSIDE it (in-container self-assertion failed)",
            "doctor routing bug - report this, and rebuild the image "
            "('booley init --force') to rule out a stale sandbox",
        )
        return

    marker = "[[CORE_RESOLVE_JSON]]"
    line = next(
        (ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith(marker)),
        None,
    )
    if line is None:
        _fail(
            ".core resolvability produced no verdict from the sandbox",
            f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}",
        )
        return
    for entry in json.loads(line[len(marker) :]):
        name = entry["name"]
        ref = refs.get(name)
        _report_core_resolve(
            name,
            bool(entry.get("ok")),
            str(entry.get("err", "")),
            is_configured(name, getattr(ref, "vlnv", "")),
            _pass,
            _warn,
            _fail,
            _note=_note,
        )


def _run_deep_checks(
    project: ProjectAudit,
    docker_exe: str | None,
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Run the real EDA smoke matrix selected by ``.core`` metadata."""
    for flow_name in _AUDITED_FLOWS:
        selection = _flow_selection(project, flow_name)
        if not selection.enabled:
            _skip(f"{flow_name} deep check skipped - disabled")
            continue
        if _execution_error(flow_name, selection):
            _skip(f"{flow_name} deep check skipped - execution selection invalid")
            continue
        targets = _doctor_targets(project, flow_name)
        if not targets:
            _skip(f"{flow_name} deep check skipped - no Doctor Target selected")
            continue
        for target in targets:
            _run_flow_check(
                project,
                flow_name,
                selection,
                target=target,
                dry_run=False,
                docker_exe=docker_exe,
                timeout_s=_deep_timeout_s(project, flow_name),
                verbose=verbose,
                _pass=_pass,
                _warn=_warn,
                _skip=_skip,
                _fail=_fail,
            )
    _run_elaborate_deep_check(
        project,
        docker_exe,
        verbose,
        _pass,
        _warn,
        _skip,
        _fail,
    )
    _run_fpga_impl_deep_notice(project, _skip)
    _run_selftest_checks(project, docker_exe, _pass, _warn, _skip, _fail)


def _run_fpga_impl_deep_notice(project: ProjectAudit, _skip: Check) -> None:
    """Disclose that ``fpga_impl`` gets no deep smoke (F-15).

    fpga_impl is deliberately excluded from :data:`_AUDITED_FLOWS` — a full FPGA
    implementation is far too slow for ``--deep`` — but it was *silently*
    absent: ``--deep`` produced no line for it at all. Nothing ever smokes it
    end-to-end, and nothing said so. Emit an
    explicit SKIP naming the manual command, so the gap is loud, not invisible.
    """
    flows = project.booley_toml.get("flows", {})
    section = flows.get("fpga") if isinstance(flows, dict) else None
    if not isinstance(section, dict):
        return
    if not _flow_selection(project, "fpga").enabled:
        return
    _skip(
        "fpga deep smoke skipped - a full FPGA implementation is too slow "
        "for --deep (binary presence is probed separately); smoke "
        "it manually end-to-end: booley flow fpga --target <fpga_target>"
    )


def _run_elaborate_deep_check(
    project: ProjectAudit,
    docker_exe: str | None,
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Deep-smoke ``elaborate`` when it is exposed (validate-or-opt-out, QA-6).

    elaborate is intentionally NOT in :data:`_AUDITED_FLOWS` (it has no execution
    menu of its own — it follows ``[flows.sim]``'s selection), so it is
    checked here rather than in the generic loop. When a project exposes
    elaborate it MUST function — a broken exposed elaborate (a generic
    FuseSoC elaborate that can't build the design) is a hard FAIL, not
    the old silent green. When a project opts out with
    ``[flows.elab].enabled = false``, that is recorded as a
    validated choice: lint/simulate cover elaboration.
    """
    if not _elaborate_active(project):
        _skip("elab deep check skipped - not exposed (opt-out; lint/sim cover elaboration)")
        return
    # elaborate follows [flows.sim]'s selection; its own section carries only
    # Flow-specific policy and the enabled opt-out handled above.
    followed = _flow_selection(project, "sim")
    selection = execution.ExecutionSelection(
        enabled=followed.enabled,
        legacy_backend=followed.legacy_backend,
    )
    if _execution_error("elab", selection):
        _skip("elab deep check skipped - the followed sim selection is invalid")
        return
    targets = _doctor_targets(project, "elab")
    if not targets:
        _skip("elab deep check skipped - no Doctor Target selected")
        return
    for target in targets:
        _run_flow_check(
            project,
            "elab",
            selection,
            target=target,
            dry_run=False,
            docker_exe=docker_exe,
            timeout_s=_deep_timeout_s(project, "elab"),
            verbose=verbose,
            _pass=_pass,
            _warn=_warn,
            _skip=_skip,
            _fail=_fail,
        )


@dataclass(frozen=True)
class _SelftestCase:
    """One convention-resolved Flow invocation used by Doctor's fail-path proof."""

    target: str
    test: str | None
    display: str


@dataclass(frozen=True)
class _SelftestPlan:
    """The known-good and known-bad invocations for one verification Flow."""

    good: _SelftestCase
    bad: _SelftestCase


def _warn_unvalidated_selftest(flow_name: str, _warn: Check) -> None:
    """Explain the missing conventional fixture for one enabled Flow."""
    if flow_name == "sim":
        requirement = "add at least one file beneath .booley_project/selftest/sim/bad-overlay/"
    else:
        requirement = f"add a known-bad .core Target named {_LINT_SELFTEST_BAD_TARGET!r}"
    _warning_sink(_warn, "flow.fail-path-unvalidated", subject=flow_name)(
        f"{flow_name} fail-path unvalidated - {requirement} so --deep proves a "
        "known-bad grades as a failure (not a false pass). The setup agent authors "
        f"the known-bad fixture. {_SELFTEST_FOOTPRINT_NOTE[flow_name]}"
    )


def _selftest_plan(
    project: ProjectAudit,
    flow_name: str,
    _warn: Check,
) -> _SelftestPlan | None:
    """Resolve conventional good/bad cases, or warn when the fixture is absent."""
    targets = _doctor_targets(project, flow_name)
    if not targets:
        return None  # The ordinary Flow target check owns this configuration failure.
    target = targets[0]
    if flow_name == "sim":
        if not selftest_overlay.has_bad_overlay(project.project_dir, flow_name):
            _warn_unvalidated_selftest(flow_name, _warn)
            return None
        test = _first_smoke_test(project, target)
        display = test or target
        return _SelftestPlan(
            good=_SelftestCase(target, test, display),
            bad=_SelftestCase(target, test, f"{display} + bad overlay"),
        )
    try:
        fusesoc_registry.resolve_ref(project.project_root, _LINT_SELFTEST_BAD_TARGET)
    except fusesoc_registry.FuseSocError:
        _warn_unvalidated_selftest(flow_name, _warn)
        return None
    return _SelftestPlan(
        good=_SelftestCase(target, None, target),
        bad=_SelftestCase(_LINT_SELFTEST_BAD_TARGET, None, _LINT_SELFTEST_BAD_TARGET),
    )


def _run_selftest_checks(
    project: ProjectAudit,
    docker_exe: str | None,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """``--deep`` fail-path self-test for the verification Flows (sim/lint).

    A pass-path smoke proves a Flow can GREEN a good design; it proves nothing
    about whether the Flow can DETECT a bad one — the entire purpose of a
    verification Flow, and the exact gap that let a false pass (QA-4) and a
    fail-path contract error (QA-5) reach a green setup. A generic doctor cannot
    manufacture a project-specific failing design, so the project (its setup
    agent) supplies conventional fixtures: a simulation bad-overlay and/or a
    lint Target named ``lint_selftest_bad``. Doctor infers the good cases from
    each Flow's first Doctor-selected Target.

    Asserted purely by the Flow exit-code contract: ``good`` => 0. ``bad`` => 1
    (fail/elab_error). A ``bad`` that exits 0 is a FALSE PASS (QA-4: stale
    artifact / ignored build rc). A ``bad`` that exits 2 is an infra/contract
    error MASKING the failure (QA-5: location-less finding turned contract_error).
    Both are hard setup failures. Absent fixtures are a WARN, not silent green:
    the fail path is simply unproven until the setup agent authors a known-bad.
    """
    for flow_name in _SELFTEST_FLOWS:
        if not _flow_selection(project, flow_name).enabled:
            continue  # disabled Flow — nothing to prove
        # Backend-agnostic since ADR 0039: the fixture mechanism proves the
        # fail path of the BUILT-IN flow exactly as it did the adapters'
        # (the C910 re-port gate found this check silently skipping builtin).
        plan = _selftest_plan(project, flow_name, _warn)
        if plan is None:
            continue
        _run_one_selftest(
            project,
            flow_name,
            plan.good,
            expect_pass=True,
            docker_exe=docker_exe,
            _pass=_pass,
            _skip=_skip,
            _fail=_fail,
        )
        _run_one_selftest(
            project,
            flow_name,
            plan.bad,
            expect_pass=False,
            docker_exe=docker_exe,
            _pass=_pass,
            _skip=_skip,
            _fail=_fail,
        )


def _prepare_selftest_invocation(
    project: ProjectAudit,
    flow_name: str,
    case: _SelftestCase,
    label: str,
    kind: str,
    *,
    docker_exe: str | None,
    _skip: Check,
    _fail: Fail,
) -> tuple[list[str], dict[str, str], int] | None:
    """Resolve the execution selection and build the argv for one self-test case.

    Returns ``None`` after already reporting a skip/fail when the case cannot
    be run at all (missing container runtime/image, or no matching Target).

    Routing mirrors :func:`_flow_check_routing`'s in-container exception:
    inside the Session Runtime there is no docker by design (ADR 0028) — this
    container IS the Session Runtime, and Flows already run as direct
    subprocesses here. Routing through docker would demand a runtime that
    cannot exist, turning the final ``--deep`` gate's fail-path proof into a
    guaranteed SKIP in-container (F-17) — so run the self-test in-place
    instead, exactly how the Flow itself executes.
    """
    from booley.runtime import runtime_context

    # Mirror _flow_check_routing: a self-test is always a REAL Flow run (never
    # a dry-run), so a host-launched deep Flow needs the sandbox image regardless
    # of backend — the old project-native-only gate ran builtin self-tests
    # bare on the host, where the toolchain doesn't exist (C910 re-port gate:
    # both good cases "failed" on a missing EDA-tool extension / Verilator, and
    # the bad cases "passed" for the wrong reason).
    use_docker = not runtime_context.inside_session_runtime()
    image = _sandbox_image(project)
    if use_docker and not docker_exe:
        _skip(f"{label} skipped - '{_CONTAINER_CLI}' runtime not available")
        return None
    if use_docker and not _docker_image_exists_by_name(image):
        _fail(
            f"{label} requires sandbox image '{image}' but it is unavailable",
            "rebuild the sandbox image (run 'booley init --force')",
        )
        return None
    cmd = _flow_command(
        project,
        flow_name,
        case.target,
        dry_run=False,
        use_docker=use_docker,
        docker_exe=docker_exe,
        image=image,
        test_override=case.test,
        doctor_selftest_kind=kind,
    )
    env = os.environ.copy()
    env["BOOLEY_PROJECT_DIR"] = str(project.project_dir)
    env[selftest_overlay.INTERNAL_KIND_ENV] = kind
    timeout_s = _deep_timeout_s(project, flow_name)
    return cmd, env, timeout_s


def _execute_selftest(
    project: ProjectAudit,
    cmd: list[str],
    env: dict[str, str],
    timeout_s: int,
    label: str,
    flow_name: str,
    *,
    _fail: Fail,
) -> subprocess.CompletedProcess | None:
    """Run the self-test subprocess, reporting infra failures as they occur.

    Returns ``None`` (having already reported a fail) on timeout, a failure to
    start, or a tripped sandbox-routing guard; otherwise the completed process
    for the caller to grade.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=project.project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _fail(
            f"{label} timed out after {timeout_s}s",
            f"raise [flows.{flow_name}].timeout_ms or use a lighter fixture",
        )
        return None
    except OSError as exc:
        _fail(f"{label} failed to start: {exc}", "check the sandbox / toolchain")
        return None
    if _sandbox_guard_failed(result):
        _fail(
            f"{label} was routed into the sandbox but executed OUTSIDE it",
            "doctor routing bug - rebuild the image ('booley init --force')",
        )
        _print_output_excerpt(result)
        return None
    return result


def _grade_selftest_result(
    result: subprocess.CompletedProcess,
    label: str,
    *,
    expect_pass: bool,
    _pass: Check,
    _fail: Fail,
) -> None:
    """Translate the self-test subprocess exit code into a doctor check verdict."""
    rc = result.returncode
    if expect_pass:
        if rc == _TOOL_EXIT_PASS:
            _pass(f"{label} passes")
        else:
            _fail(
                f"{label} did not pass (exit {rc})",
                "the 'good' fixture must pass - fix the Flow config or pick a passing case",
            )
            _print_output_excerpt(result)
        return
    # bad case: must be a GRADED design failure (exit 1) — never a pass or infra error.
    if rc == _TOOL_EXIT_DESIGN_FAIL:
        _pass(f"{label} correctly graded a failure")
    elif rc == _TOOL_EXIT_PASS:
        _fail(
            f"{label} FALSE-PASSED (exit 0) on a known-bad design",
            "the Flow reports pass on a failing build - it is trusting a "
            "stale artifact or ignoring the build exit code (QA-4)",
        )
        _print_output_excerpt(result)
    else:
        _fail(
            f"{label} returned an infra error (exit {rc}) instead of a graded failure",
            "the fail-path response likely violates the finding contract (e.g. a "
            "finding with an empty 'file'); sanitize lint findings so a real "
            "failure grades as 'fail', not contract_error (QA-5)",
        )
        _print_output_excerpt(result)


def _run_one_selftest(
    project: ProjectAudit,
    flow_name: str,
    case: _SelftestCase,
    *,
    expect_pass: bool,
    docker_exe: str | None,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    kind = "good" if expect_pass else "bad"
    label = f"{flow_name} self-test {kind} case '{case.display}'"
    prepared = _prepare_selftest_invocation(
        project,
        flow_name,
        case,
        label,
        kind,
        docker_exe=docker_exe,
        _skip=_skip,
        _fail=_fail,
    )
    if prepared is None:
        return
    cmd, env, timeout_s = prepared
    result = _execute_selftest(project, cmd, env, timeout_s, label, flow_name, _fail=_fail)
    if result is None:
        return
    _grade_selftest_result(result, label, expect_pass=expect_pass, _pass=_pass, _fail=_fail)


def _flow_class(flow_name: str) -> Any:
    """The Booley Flow class owning *flow_name*'s execution matrix."""
    import importlib

    module_name, class_name = _EXECUTION_VALIDATING_FLOWS[flow_name]
    return getattr(importlib.import_module(module_name), class_name)


def _flow_selection(project: ProjectAudit, flow_name: str) -> execution.ExecutionSelection:
    """Build *flow_name*'s ``ExecutionSelection`` from the parsed booley.toml.

    Reads the dict the audit already loaded instead of re-reading config from
    disk. A legacy backend is retained only for its hard-migration error.
    """
    flows = project.booley_toml.get("flows", {})
    if not isinstance(flows, dict):
        flows = {}
    section = config_section(flows, flow_name)
    raw_backend = section.get("backend")
    return execution.ExecutionSelection(
        enabled=section.get("enabled", True) is not False,
        legacy_backend=None if raw_backend is None else str(raw_backend).strip(),
    )


def _elaborate_active(project: ProjectAudit) -> bool:
    """Whether elaborate is enabled and therefore must be validated (QA-6)."""
    return _flow_selection(project, "elab").enabled


def _deep_timeout_s(project: ProjectAudit, flow_name: str) -> int:
    """Wall-clock budget for a ``--deep`` Flow smoke, in seconds (F5).

    The hardcoded :data:`_DEEP_TIMEOUTS_S` floor is a *minimum*, not the
    authority: a project that raised ``[flows.<flow>].timeout_ms`` (e.g. a
    415K-LOC core needing 90-min asic synth, or a heavy sim) would otherwise be
    spuriously killed by the shorter deep budget. Honor the configured knob by
    taking the larger of the two, so deep never falls below the safe smoke floor
    yet respects a legitimately-raised per-Flow timeout. Unparseable values are
    ignored and the floor stands.
    """
    timeout_s = _configured_timeout_s(project, flow_name, _DEEP_TIMEOUTS_S[flow_name])
    if flow_name == "synth":
        timeout_s += _SYNTH_DEEP_FINALIZE_MARGIN_S
    return timeout_s


def _configured_timeout_s(project: ProjectAudit, flow_name: str, floor: int) -> int:
    """``max(floor, [flows.<flow>].timeout_ms)`` in seconds; the floor stands
    when the knob is absent or unparseable."""
    flows = project.booley_toml.get("flows", {})
    section = config_section(flows, flow_name) if isinstance(flows, dict) else {}
    if not isinstance(section, dict):
        return floor
    raw = section.get("timeout_ms")
    if raw is None:
        return floor
    try:
        configured_s = int(raw) // 1000
    except (TypeError, ValueError):
        return floor
    return max(floor, configured_s)


def _execution_error(flow_name: str, selection: execution.ExecutionSelection) -> str | None:
    """Reject a retired ``backend`` through the Flow's shared validator.

    Principle 9: delegates to ``BooleyFlow.validate_execution`` (each Flow declares
    the run entry point emit the identical migration error.
    """
    return _flow_class(flow_name).validate_execution(selection)


def _flow_check_routing(
    project: ProjectAudit,
    flow_name: str,
    selection: execution.ExecutionSelection,
    *,
    dry_run: bool,
) -> tuple[bool, str, str]:
    """Return ``(use_docker, image, label)`` for a Flow check.

    Deep checks execute the real Flow against a resolved .core Target, which
    needs fusesoc/edalize and the toolchain — that environment is the Session
    Runtime image, not the host. Cheap dry-runs stay on the host.

    Exception: inside the Session Runtime there is no docker by design (ADR
    0028) — this container is the Session Runtime, and Flows already
    run natively here (Phase B of QA_REPORT ran simulate/lint/synth to
    completion in exactly this runtime). Routing to docker here would demand a
    'booley-sandbox' image that cannot exist in-container, turning the highest-
    value health check into a guaranteed red herring with dead-end advice
    ('booley init --force', install docker) — QA_REPORT A5. Run the deep check
    natively instead, mirroring how the Flows themselves execute.
    """
    from booley.runtime import runtime_context

    in_container = runtime_context.inside_session_runtime()
    use_docker = not in_container and not dry_run
    image = _sandbox_image(project)
    label = f"{flow_name} {'dry-run' if dry_run else 'deep check'}"
    return use_docker, image, label


def _guard_docker_availability(
    *,
    use_docker: bool,
    docker_exe: str | None,
    image: str,
    label: str,
    _skip: Check,
    _fail: Fail,
) -> bool:
    """Report SKIP/FAIL for a broken docker routing; ``True`` means continue."""
    if use_docker and not docker_exe:
        # No container runtime on this host at all — an unavailable runtime, not a
        # broken build. Mirror the sibling checks (container / interactive), which
        # SKIP for exactly this condition: "cannot run here", not "setup broken".
        # Suggesting 'booley init --force' would be noise when there's no runtime.
        _skip(
            f"{label} skipped - '{_CONTAINER_CLI}' runtime not available "
            "(sandbox check cannot run here)"
        )
        return False
    if use_docker and not _docker_image_exists_by_name(image):
        # The runtime IS present but the sandbox image is missing/unbuilt: this
        # check MUST run in the sandbox and genuinely can't. b5e8681's failure
        # class — a silent SKIP here let a broken/missing image read as a healthy
        # setup (the check never ran anywhere) — so it is a hard FAIL.
        _fail(
            f"{label} requires the sandbox image '{image}' but the image is "
            "unavailable - the check cannot run",
            "rebuild the sandbox image (run 'booley init --force')",
        )
        return False
    return True


def _display_report_dir(project: ProjectAudit, report_dir: Path) -> str:
    """Render *report_dir* so the hint is valid where it is READ, not where run.

    Doctor may run inside the Session Runtime, where the project dir is the
    ``/booley-project`` bind mount — a path that exists in no other runtime, while
    the FAIL hint is often read from the host (where the same files sit under
    ``<repo>/.booley_project/``). For that well-known mount, print the
    repo-relative rendering that resolves on both host and runtime; any other project dir
    (host default, or an explicit ``[project].dir`` override) is printed as-is.
    """
    if project.project_dir.as_posix() != dc.PROJECT_DIR_TARGET:
        return str(report_dir)
    try:
        rel = report_dir.relative_to(project.project_dir)
    except ValueError:
        return str(report_dir)
    return f"{PROJECT_DIR_NAME}/{rel.as_posix()} (under the repo root)"


def _run_flow_check_subprocess(
    project: ProjectAudit,
    cmd: list[str],
    *,
    timeout_s: int,
    label: str,
    report_dir: Path,
    _fail: Fail,
) -> subprocess.CompletedProcess | None:
    """Run *cmd* for a Flow check; ``None`` means a FAIL was already reported."""
    env = os.environ.copy()
    env["BOOLEY_PROJECT_DIR"] = str(project.project_dir)
    try:
        return subprocess.run(
            cmd,
            cwd=project.project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _fail(
            f"{label} timed out after {timeout_s}s",
            f"see {_display_report_dir(project, report_dir)}",
        )
        return None
    except OSError as exc:
        _fail(
            f"{label} failed to start: {exc}",
            f"see {_display_report_dir(project, report_dir)}",
        )
        return None


def _interpret_flow_check_result(
    project: ProjectAudit,
    flow_name: str,
    selection: execution.ExecutionSelection,
    result: subprocess.CompletedProcess,
    *,
    target: str,
    dry_run: bool,
    verbose: bool,
    label: str,
    report_dir: Path,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Translate a Flow check's subprocess result into a PASS/WARN/SKIP/FAIL."""
    if _sandbox_guard_failed(result):
        # The in-container self-assertion refused to run: a command composed
        # for the sandbox executed somewhere WITHOUT the sandbox environment.
        # This is doctor misrouting (b5e8681's failure class), never a project
        # problem — surface it loudly instead of letting the Flow's own exit
        # code masquerade as an ordinary check failure.
        _fail(
            f"{label} was routed into the sandbox but executed OUTSIDE it "
            "(in-container self-assertion failed)",
            "doctor routing bug - a sandbox-semantics check ran on the host; "
            "report this, and rebuild the image ('booley init --force') to rule "
            "out a stale sandbox",
        )
        _print_output_excerpt(result)
        return

    _report_flow_check_result(
        project,
        flow_name,
        selection,
        result,
        target=target,
        dry_run=dry_run,
        label=label,
        report_dir=report_dir,
        verbose=verbose,
        _pass=_pass,
        _warn=_warn,
        _skip=_skip,
        _fail=_fail,
    )


def _report_flow_check_result(
    project: ProjectAudit,
    flow_name: str,
    selection: execution.ExecutionSelection,
    result: subprocess.CompletedProcess[str],
    *,
    target: str,
    dry_run: bool,
    label: str,
    report_dir: Path,
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Turn a finished Flow run into a doctor verdict.

    The one question this smoke check exists to answer is "can this Flow run
    against this project at all" — so a *graded* nonzero exit is not
    automatically a setup defect (see :func:`_is_lint_findings_exit`).
    """
    synth_error = _synth_deep_report_error(flow_name, target, dry_run, report_dir)
    if result.returncode == 0 and synth_error:
        _fail(
            f"{label} produced incomplete synthesis evidence: {synth_error}",
            f"see {_display_report_dir(project, report_dir)}",
        )
    elif result.returncode == 0:
        _pass(f"{label} passed")
        if verbose:
            _print_output_excerpt(result, max_lines=1)
    elif _is_simulate_tb_top_skip(project, flow_name, dry_run, result):
        _skip(
            f"{label} skipped - tb_top is resolved at runtime "
            "(no static tb_top in the first config)"
        )
    elif _is_lint_findings_exit(flow_name, result):
        # Lint findings are a project-quality verdict, not a setup defect: the
        # flow demonstrably ran end to end (it produced a graded WARN). Faulting
        # them made --deep unpassable for any project with a single lint warning
        # — the FAIL block would literally embed "RESULT: WARN (58 warnings)" —
        # which says nothing about whether lint is wired up correctly, the only
        # question this smoke check exists to answer.
        _pass(
            f"{label} ran end-to-end; the design has lint findings "
            f"(exit {_TOOL_EXIT_DESIGN_FAIL} = WARN), which belong to `booley lint`"
        )
        if verbose:
            _print_output_excerpt(result)
    else:
        _fail(
            f"{label} failed with exit {result.returncode}",
            f"see {_display_report_dir(project, report_dir)}",
        )
        _print_output_excerpt(result)


def _synth_deep_report_error(flow_name: str, target: str, dry_run: bool, report_dir: Path) -> str:
    """Return why a successful deep synth lacks terminal PPA/timing evidence."""
    if flow_name != "synth" or dry_run:
        return ""
    from booley.flows.synth.flow import synth_target_report_slug

    path = report_dir / f"synth_{synth_target_report_slug(target)}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"report is unreadable ({exc})"
    if report.get("ppa_complete") is not True:
        return "ppa_complete is not true"
    if report.get("timing_complete") is not True or not report.get("per_clock"):
        return "no completed constrained-clock timing result"
    return ""


def _is_lint_findings_exit(
    flow_name: str,
    result: subprocess.CompletedProcess[str],
) -> bool:
    """True when lint exited "findings present" rather than "lint is broken".

    Lint uses exit 1 for both warning findings and fatal design parse/elaboration
    errors. Its stable report marker distinguishes them: only the former says
    ``RESULT: WARN``. Treating every exit 1 as advisory hid an undefined-signal
    ``%Error`` behind a green doctor run (Taxi F-31).
    """
    output = f"{result.stdout}\n{result.stderr}"
    return (
        flow_name == "lint"
        and result.returncode == _TOOL_EXIT_DESIGN_FAIL
        and "RESULT: WARN" in output
    )


def _run_flow_check(
    project: ProjectAudit,
    flow_name: str,
    selection: execution.ExecutionSelection,
    *,
    target: str,
    dry_run: bool,
    docker_exe: str | None,
    timeout_s: int,
    verbose: bool,
    _pass: Check,
    _warn: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    use_docker, image, label = _flow_check_routing(
        project,
        flow_name,
        selection,
        dry_run=dry_run,
    )
    label = f"{label} [{target}]"
    if not _guard_docker_availability(
        use_docker=use_docker,
        docker_exe=docker_exe,
        image=image,
        label=label,
        _skip=_skip,
        _fail=_fail,
    ):
        return

    report_dir = project.project_dir / _DOCTOR_TMP / "flow-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if flow_name == "synth" and not dry_run:
        from booley.flows.synth.flow import synth_target_report_slug

        safe_target = synth_target_report_slug(target)
        # A killed calibration may never reach the Flow's eager per-target
        # writer. Remove the previous flat copy so post-run calibration cannot
        # mistake an older passing Target report for this attempt.
        with contextlib.suppress(OSError):
            (report_dir / f"synth_{safe_target}.json").unlink(missing_ok=True)
    cmd = _flow_command(
        project,
        flow_name,
        target,
        dry_run=dry_run,
        use_docker=use_docker,
        docker_exe=docker_exe,
        image=image,
    )
    result = _run_flow_check_subprocess(
        project,
        cmd,
        timeout_s=timeout_s,
        label=label,
        report_dir=report_dir,
        _fail=_fail,
    )
    if result is None:
        return

    _interpret_flow_check_result(
        project,
        flow_name,
        selection,
        result,
        target=target,
        dry_run=dry_run,
        verbose=verbose,
        label=label,
        report_dir=report_dir,
        _pass=_pass,
        _warn=_warn,
        _skip=_skip,
        _fail=_fail,
    )
    if flow_name == "synth" and not dry_run:
        _record_synth_memory_calibration(
            project,
            target,
            selection,
            report_dir,
            _pass,
            _warn,
        )


def _record_synth_memory_calibration(
    project: ProjectAudit,
    target: str,
    selection: execution.ExecutionSelection,
    report_dir: Path,
    _pass: Check,
    _warn: Check,
) -> None:
    """Record boundary-process peak RSS from a completed Doctor synthesis."""
    from booley.flows.synth.flow import synth_target_report_slug

    safe_target = synth_target_report_slug(target)
    path = report_dir / f"synth_{safe_target}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return  # the Flow failure above owns the missing-report diagnostic
    if not isinstance(report, dict) or not report.get("ppa_complete"):
        return
    peak = report.get("peak_rss_mb")
    if isinstance(peak, bool) or not isinstance(peak, (int, float)) or peak <= 0:
        _warning_sink(_warn, "synth.memory-calibration-unavailable", subject=target)(
            f"synthesis Target {target} completed, but its process-tree "
            "peak RSS could not be measured; [jobs].heavy_memory remains uncalibrated"
        )
        return
    from booley.harness import synth_probe

    synth_probe.record_measurement(
        project.project_dir,
        target,
        float(peak),
        selected_targets=_doctor_targets(project, "synth"),
    )
    reservation = _heavy_memory_reservation(project)
    _pass(
        f"synth memory calibrated: {target} peaked at {float(peak) / 1024:.1f}g; "
        f"HEAVY reservation {resource_policy.format_memory(reservation.bytes)} "
        f"({reservation.evidence})"
    )
    # The runtime-phase invariant ran before the deep synthesis created this
    # measurement. Re-evaluate now so the same Doctor invocation cannot finish
    # green on the stale 4 GiB fallback.
    _check_memory_invariant(project, _pass, _warn, lambda _message: None)


def _is_simulate_tb_top_skip(
    project: ProjectAudit,
    flow_name: str,
    dry_run: bool,
    result: subprocess.CompletedProcess[str],
) -> bool:
    """Recognize the stateless dry-run tb_top limitation as a skip, not a fail.

    A stateless ``simulate --dry-run`` cannot resolve tb_top from run state. When
    no config pins tb_top statically, the Flow exits non-zero with a --tb-top
    error that does not reflect a real misconfiguration; real state-backed runs
    resolve tb_top from the sim Target's toplevel (``tb_top_for_target``).
    """
    if flow_name != "sim" or not dry_run:
        return False
    config = project.configs_toml.get(project.first_target, {})
    if config.get("tb_top"):
        return False
    needle = "--tb-top is required"
    if needle in f"{result.stderr or ''}\n{result.stdout or ''}":
        return True
    report = project.project_dir / _DOCTOR_TMP / "flow-reports" / f"{flow_name}.json"
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return needle in str(payload.get("report_text", ""))


def _doctor_targets(project: ProjectAudit, flow_name: str) -> list[str]:
    """Every ``.core`` Target that explicitly selects one Doctor Flow."""
    return list(target_matrix.doctor_targets(project.project_root, flow_name))


def _doctor_target_seed(project: ProjectAudit) -> list[str]:
    """The deduplicated Target surface that Doctor gates and audits."""
    return list(_project_target_matrix(project).seed_targets)


def _project_target_matrix(project: ProjectAudit) -> target_matrix.DoctorTargetMatrix:
    """Build the domain view consumed by Doctor's target orchestration."""
    return target_matrix.build_doctor_target_matrix(project.project_root)


def _check_doctor_targets(project: ProjectAudit, flow_name: str, _fail: Fail) -> list[str]:
    """Validate and return the Target matrix selected for *flow_name*."""
    targets = _doctor_targets(project, flow_name)
    if not targets:
        try:
            available = fusesoc_registry.available_targets(project.project_root)
        except fusesoc_registry.FuseSocError:
            available = []
        candidates = f"; available Targets: {', '.join(available)}" if available else ""
        _fail(
            f"{flow_name} has no Doctor Target{candidates}",
            "add the Flow name to a compatible .core Target's "
            "flow_options.booley.doctor list (or disable the Flow in booley.toml)",
        )
        return []
    valid: list[str] = []
    for target in targets:
        try:
            ref = fusesoc_registry.resolve_ref(project.project_root, target)
        except fusesoc_registry.FuseSocError as exc:
            _fail(f"{flow_name} Doctor Target {target!r} does not resolve: {exc}", "fix the .core")
            continue
        failure = _doctor_target_incompatibility(target, flow_name, ref)
        if failure:
            _fail(*failure)
            continue
        valid.append(target)
    return valid


def _check_elaborate_setup(
    project: ProjectAudit,
    _pass: Check,
    _skip: Check,
    _fail: Fail,
) -> None:
    """Cheap-pass audit for ``elaborate`` (validate-or-opt-out, QA-6 / F-9).

    elaborate is absent from :data:`_AUDITED_FLOWS` (no execution menu of its
    own), so the generic loop in :func:`_run_flow_audit` never sees it. It is
    still on the MCP surface by default, and its Target requirement is identical
    to the Booley Flows' — so audit it here rather than leaving the gap for
    ``--deep``. There is no dry-run smoke for elaborate; the deep check
    (:func:`_run_elaborate_deep_check`) still owns "does it actually build".
    """
    if not _elaborate_active(project):
        _skip("elab disabled in booley.toml (opt-out; lint/sim cover elaboration)")
        return
    targets = _check_doctor_targets(project, "elab", _fail)
    if targets:
        _pass(f"elab Doctor Targets: {', '.join(targets)}")


def _first_smoke_test(project: ProjectAudit, target: str) -> str | None:
    """The one test the deep simulate smoke should pin for *target*.

    Without a ``--test``, the Simulation Flow runs the Target's WHOLE test list —
    on a large design that blows the smoke budget (openc910: 16 full-chip C910
    sims vs. one ~190 s case) and turns the deep check into a spurious timeout
    FAIL. tests.toml is the modern per-Target test registry (ADR 0022 retired
    configs.toml), so read it first, honoring the ``skip`` list — the first
    *runnable* test, not a known-hang or a deliberately-failing selftest
    fixture. Falls back to the legacy configs.toml entry for pre-migration
    projects, then to None (no --test: the Target's single/default run).
    """
    tests_path = project.project_dir / "tests.toml"
    if tests_path.is_file():
        try:
            with tests_path.open("rb") as f:
                sections = normalize_tests_toml(tomllib.load(f))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            sections = {}  # _audit_tests_toml already reported the breakage
        # Targets may arrive VLNV-qualified ("vlnv#name"); tests.toml keys are
        # bare Target names.
        section = sections.get(target) or sections.get(target.rsplit("#", 1)[-1])
        if isinstance(section, dict):
            tests = section.get("tests")
            if isinstance(tests, list) and tests:
                skips = set(section.get("skip") or [])
                runnable = [t for t in tests if t not in skips]
                # All-skip misconfig: fall back to the declared head rather
                # than silently smoke nothing (mirrors the Simulation Flow).
                return (runnable or tests)[0]
    config = project.configs_toml.get(project.first_target, {})
    tests = config.get("tests")
    if isinstance(tests, list) and tests and isinstance(tests[0], str):
        return tests[0]
    return None


def _flow_command(
    project: ProjectAudit,
    flow_name: str,
    target: str,
    *,
    dry_run: bool,
    use_docker: bool,
    docker_exe: str | None,
    image: str,
    test_override: str | None = None,
    doctor_selftest_kind: str | None = None,
) -> list[str]:
    work_dir = "/work" if use_docker else str(project.project_root)
    report_dir = (
        "/work/.booley_project/tmp/doctor/flow-reports"
        if use_docker
        else str(project.project_dir / _DOCTOR_TMP / "flow-reports")
    )
    argv = [
        "--work-dir",
        work_dir,
        "--report-dir",
        report_dir,
        # The built-in Booley Flows (simulate/lint/asic_synthesize) take --target
        # (ADR 0022); doctor probes them with a flow-matched .core Target.
        "--target",
        target,
    ]
    if dry_run:
        argv.extend(["--dry-run", "--timeout", "30000"])

    if flow_name == "sim":
        # tb_top comes from the resolved Target now; only --test stays on the
        # surface. asic_synthesize/fpga_impl take their top from the Target too.
        # A self-test pins the exact test (test_override); otherwise probe the
        # first configured test.
        if test_override is not None:
            argv.extend(["--test", test_override])
        else:
            first = _first_smoke_test(project, target)
            if first:
                argv.extend(["--test", first])

    from booley.targets.flow_names import implementation_module

    inner = [
        "python3" if use_docker else sys.executable,
        "-m",
        f"booley.flows.{implementation_module(flow_name)}",
        *argv,
    ]
    if not use_docker:
        return inner

    if docker_exe is None:
        return inner
    return _docker_wrap(
        docker_exe,
        image,
        project.project_root,
        inner,
        memory=resource_policy.configured_sandbox_memory(project.booley_toml),
        doctor_selftest_kind=doctor_selftest_kind,
    )


# In-container self-assertion (b5e8681's failure class: sandbox-semantics
# checks silently running on the host). Every inner command routed through
# _docker_wrap is prefixed with a POSIX-sh guard that re-verifies the command
# really IS inside the sandbox before exec'ing it. If a wrapped command ever
# executes outside the container (misrouting, argv recomposition dropping the
# docker prefix), the guard refuses with a distinctive exit code + stderr
# marker that callers convert into a loud FAIL — never a silent pass or SKIP.
_SANDBOX_GUARD_MARKER = "[[BOOLEY_SANDBOX_GUARD_FAIL]]"
# Distinctive code so callers can tell "guard refused" apart from ordinary Flow
# failures (Flows exit 0/1/2) and from Docker's own 125-127 range.
_SANDBOX_GUARD_EXIT = 97
# The assertion is deliberately env-only, NOT a marker-file probe
# (/.dockerenv): BOOLEY_IN_SANDBOX=1 is
# injected by _docker_wrap's own ``-e`` below, so it proves the command went
# through THIS wrapper's docker invocation. A /.dockerenv fallback would
# falsely pass when doctor itself runs inside the Interactive-Mode
# devcontainer and a misrouted "sandbox" check lands there instead of the
# real sandbox image.
_SANDBOX_GUARD_SCRIPT = (
    'if [ "$BOOLEY_IN_SANDBOX" = "1" ]; then exec "$@"; fi; '
    f'echo "{_SANDBOX_GUARD_MARKER} refusing to run: not inside the sandbox '
    "(BOOLEY_IN_SANDBOX unset - the command did not go through doctor's "
    'docker wrapper)" >&2; '
    f"exit {_SANDBOX_GUARD_EXIT}"
)


def _sandbox_guard_failed(result: subprocess.CompletedProcess[str]) -> bool:
    """Return whether *result* is the sandbox guard refusing a misrouted run.

    Requires BOTH the reserved exit code and the stderr marker so a Flow that
    happens to exit 97 for its own reasons is not misreported as misrouting.
    """
    if result.returncode != _SANDBOX_GUARD_EXIT:
        return False
    return _SANDBOX_GUARD_MARKER in f"{result.stderr or ''}{result.stdout or ''}"


def _docker_wrap(
    docker_exe: str,
    image: str,
    project_root: Path,
    inner: list[str],
    *,
    memory: str = "",
    doctor_selftest_kind: str | None = None,
) -> list[str]:
    """Wrap *inner* argv in a ``docker run`` that mounts the repo at ``/work``.

    The single source of truth for how doctor executes a command inside the
    Sandbox image: repo bind-mounted at ``/work``, network isolated, and the
    in-container ``BOOLEY_PROJECT_DIR`` / ``BOOLEY_IN_SANDBOX`` env set so the
    Flow resolves the project the same way a real sandbox run would.

    The inner argv is additionally prefixed with the sandbox self-assertion
    guard (see ``_SANDBOX_GUARD_SCRIPT``): *inner* is passed as positional
    parameters to ``sh -c '... exec "$@"'`` — never spliced into the script
    string — so arbitrary argv (including ``python3 -c`` snippets) survives
    unquoted, and every command wrapped here gets the assertion for free.
    """
    resource_args = ["--memory", memory] if memory else []
    selftest_env = (
        ["-e", f"{selftest_overlay.INTERNAL_KIND_ENV}={doctor_selftest_kind}"]
        if doctor_selftest_kind is not None
        else []
    )
    return [
        docker_exe,
        "run",
        "--init",
        "--rm",
        *resource_args,
        "-v",
        f"{docker_mount_path(project_root)}:/work",
        "-w",
        "/work",
        "--network",
        "none",
        "-e",
        "BOOLEY_PROJECT_DIR=/work/.booley_project",
        "-e",
        "BOOLEY_IN_SANDBOX=1",
        *selftest_env,
        image,
        # $0 for the guard shell; *inner* lands in "$@" untouched.
        "sh",
        "-c",
        _SANDBOX_GUARD_SCRIPT,
        "booley-sandbox-guard",
        *inner,
    ]


# Substrings that mark the informative failure lines in EDA/fusesoc output. The
# reason for a fusesoc parse error is printed on the line AFTER
# "Parse error. Ignoring file X:", so a first-line-only excerpt dropped it.
_OUTPUT_ERROR_MARKERS = (
    "error",
    "must be array",
    "ignoring file",
    "could not",
    "cannot",
    "traceback",
    "failed",
    "exception",
    "not found",
    "requires",
)


def _print_text_excerpt(text: str, max_lines: int = 8) -> None:
    """Print the informative slice of multi-line command output (indented).

    A first-line-only excerpt truncated multi-line diagnostics — notably a
    fusesoc parse error whose reason (``... must be array``) lands on the *next*
    line. When any line looks like an error, print from the first such line
    through the line after the last one (to catch the trailing reason); otherwise
    fall back to the head of the output. Capped at *max_lines* with a "… N more"
    note so the report dir is still the full record.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return
    flagged = [
        i for i, ln in enumerate(lines) if any(m in ln.lower() for m in _OUTPUT_ERROR_MARKERS)
    ]
    if flagged and max_lines > 1:
        selected = lines[flagged[0] : flagged[-1] + 2][:max_lines]
    else:
        selected = lines[:max_lines]
    for ln in selected:
        info(f"    {ln}")
    hidden = len(lines) - len(selected)
    if hidden > 0:
        info(f"    … ({hidden} more line(s); see the report dir)")


def _print_output_excerpt(result: subprocess.CompletedProcess[str], max_lines: int = 8) -> None:
    """Print the informative slice of a failed command's output (:func:`_print_text_excerpt`)."""
    _print_text_excerpt(result.stderr or result.stdout or "", max_lines)
