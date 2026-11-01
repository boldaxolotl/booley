"""Console adapter for the Project-independent Host Bootstrap module."""

from __future__ import annotations

from pathlib import Path

from booley.harness.bootstrap import (
    BootstrapFinding,
    BootstrapResult,
    BootstrapState,
    inspect_bootstrap,
    reconcile_bootstrap,
)
from booley.harness.colors import green, red, yellow
from booley.harness.terminal_status import LineStatus, section_header, status_line
from booley.runtime.host_install import HostInstallationError, register_host_installation
from booley.runtime.image_lifecycle import Intent
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.paths import skills_dir
from booley.runtime.qa_skill_selection import QaSkillSelectionError
from booley.runtime.qa_skill_selection import (
    disable as disable_qa_skills,
)
from booley.runtime.qa_skill_selection import (
    enable as enable_qa_skills,
)


def run_bootstrap(args: object) -> int:
    """Run Host Bootstrap and render its typed findings."""
    update = getattr(args, "update", False)
    with_qa = getattr(args, "with_qa_skills", False)
    without_qa = getattr(args, "without_qa_skills", False)
    check_only = getattr(args, "check_only", False)
    if update and check_only:
        print(red("--update cannot be combined with --check-only"))
        return 2
    if check_only and (with_qa or without_qa):
        print(red("QA skill selection flags cannot be combined with --check-only"))
        return 2
    intent = (
        Intent.CHECK
        if check_only
        else Intent.REFRESH
        if getattr(args, "force", False)
        else Intent.ENSURE
    )
    result = (
        _check_bootstrap(args)
        if intent is Intent.CHECK
        else _mutate_bootstrap(args, intent, update, with_qa, without_qa)
    )
    if result is None:
        return 2
    status = _render_result(result)
    if (with_qa or without_qa) and _qa_warning(result):
        return 2
    return status


def _check_bootstrap(args: object) -> BootstrapResult:
    from booley.runtime import issuance_invalidation, session_runtime
    from booley.runtime.session_refresh import shared_recovery_blocks_command

    findings: tuple[BootstrapFinding, ...] = ()
    try:
        recovery_pending = shared_recovery_blocks_command(read_only=True)
    except (issuance_invalidation.InvalidationError, session_runtime.SessionError) as exc:
        findings = _recovery_error(Intent.CHECK, exc).findings
    else:
        if recovery_pending:
            findings = (
                BootstrapFinding(
                    "runtime-recovery",
                    BootstrapState.PENDING,
                    "Interrupted Sandbox host state requires recovery; run `booley bootstrap` "
                    "to attempt recovery, then run it again",
                ),
            )
    result = inspect_bootstrap(verbose=getattr(args, "verbose", False))
    return BootstrapResult(
        Intent.CHECK, (*findings, *result.findings), result.policy, result.base_image
    )


def _mutate_bootstrap(
    args: object,
    intent: Intent,
    update: bool,
    with_qa: bool,
    without_qa: bool,
) -> BootstrapResult | None:
    from booley.runtime import issuance_invalidation, session_runtime
    from booley.runtime.session_refresh import shared_recovery_blocks_command

    with host_lifecycle_lock("host bootstrap"):
        try:
            recovery_performed = shared_recovery_blocks_command(read_only=False)
        except (issuance_invalidation.InvalidationError, session_runtime.SessionError) as exc:
            return _recovery_error(intent, exc)
        if recovery_performed:
            print(
                yellow("Recovered interrupted Sandbox host state; run `booley bootstrap` again.")
            )
            return None
        try:
            identity = register_host_installation(skills_dir(), update=update)
        except HostInstallationError as exc:
            print(red(f"Cannot register host installation: {exc}"))
            return None
        if update:
            print(
                green(
                    "Updated canonical Booley host installation "
                    f"{identity.version} ({identity.payload_fingerprint[:12]})."
                )
            )
        if not _apply_qa_enable(with_qa):
            return None
        result = reconcile_bootstrap(
            intent,
            verbose=getattr(args, "verbose", False),
            qa_opt_out=without_qa,
        )
        if without_qa and _qa_prune_succeeded(result):
            try:
                disable_qa_skills()
            except OSError as exc:
                print(red(f"Cannot disable QA skills: {exc}"))
                return None
        return result


def _recovery_error(intent: Intent, error: RuntimeError) -> BootstrapResult:
    return BootstrapResult(
        intent,
        (BootstrapFinding("runtime-recovery", BootstrapState.ERROR, str(error)),),
    )


def _apply_qa_enable(with_qa: bool) -> bool:
    if not with_qa:
        return True
    try:
        enable_qa_skills(Path.cwd())
    except (QaSkillSelectionError, OSError) as exc:
        print(red(f"Cannot enable QA skills: {exc}"))
        return False
    return True


def _qa_prune_succeeded(result: BootstrapResult) -> bool:
    return any(
        finding.resource == "qa-skills"
        and finding.state in {BootstrapState.CURRENT, BootstrapState.CHANGED}
        for finding in result.findings
    )


_ROW_STATUS = {
    BootstrapState.CURRENT: LineStatus.CURRENT,
    BootstrapState.PENDING: LineStatus.PENDING,
    BootstrapState.CHANGED: LineStatus.PASS,
    BootstrapState.WARNING: LineStatus.WARN,
    BootstrapState.ERROR: LineStatus.FAIL,
}


def _render_result(result: BootstrapResult) -> int:
    print(section_header("Host Bootstrap"))
    for finding in result.findings:
        detail = _inspection_detail(finding) if result.intent is Intent.CHECK else finding.detail
        print(status_line(_ROW_STATUS[finding.state], f"{finding.resource}: {detail}"))
    if result.exit_status == 0:
        print(green("Host Bootstrap is current."))
    elif result.exit_status == 1:
        print(yellow("Host Bootstrap has pending work; run `booley bootstrap`."))
    else:
        print(red("Host Bootstrap is incomplete; fix the errors above and retry."))
    return result.exit_status


def _qa_warning(result: BootstrapResult) -> bool:
    return any(
        finding.resource == "qa-skills" and finding.state is BootstrapState.WARNING
        for finding in result.findings
    )


def _inspection_detail(finding: BootstrapFinding) -> str:
    """Attach Bootstrap remedies without changing legacy Doctor diagnostics."""
    if finding.state is BootstrapState.PENDING:
        suffix = "" if "booley bootstrap" in finding.detail else "; run `booley bootstrap`"
        return finding.detail + suffix
    if finding.state is not BootstrapState.ERROR:
        return finding.detail
    if finding.remedy is not None:
        return finding.detail + (f"; {finding.remedy}" if finding.remedy else "")
    remedies = {
        "host-config": "repair the host config at the path above and retry",
        "host-install": "restore read access to the recorded identity or reinstall a valid canonical wheel",
        "git": "install supported Git and put git on PATH, then retry",
        "docker": "install Docker on PATH, start its daemon and restore Docker access, then retry",
        "vscode": "install a supported VS Code editor and its shell command, then retry",
        "vscode-dev-containers": "restore access to the editor extension-management command and retry",
        "skills": "repair access to the named packaged skills or skill-link manifest and retry",
        "nangate45": "restore read access to the named cache paths and retry",
        "runtime-recovery": "repair the journal permissions or unsafe state described above and retry",
    }
    remedy = remedies.get(
        finding.resource,
        "restore Docker inspection access, reinstall missing package inputs, or remove/rename the named foreign Docker resource after checking ownership, then retry",
    )
    return f"{finding.detail}; {remedy}"
