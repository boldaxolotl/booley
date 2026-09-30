"""Console adapter for the Project-independent Host Bootstrap module."""

from __future__ import annotations

from booley.harness.bootstrap import (
    BootstrapFinding,
    BootstrapResult,
    BootstrapState,
    reconcile_bootstrap,
)
from booley.harness.colors import accent, bold_chrome, green, red, yellow
from booley.runtime.host_install import HostInstallationError, register_host_installation
from booley.runtime.image_lifecycle import Intent
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.paths import skills_dir
from booley.runtime.qa_skill_selection import (
    QaSkillSelectionError,
    checkout_enclosing_cwd,
)
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


def _check_bootstrap(args: object) -> BootstrapResult | None:
    from booley.runtime import issuance_invalidation, session_runtime
    from booley.runtime.session_refresh import shared_recovery_blocks_command

    try:
        recovery_pending = shared_recovery_blocks_command(read_only=True)
    except (issuance_invalidation.InvalidationError, session_runtime.SessionError) as exc:
        return _recovery_error(Intent.CHECK, exc)
    if recovery_pending:
        print(yellow("Interrupted Sandbox host state requires recovery."))
        return None
    return reconcile_bootstrap(Intent.CHECK, verbose=getattr(args, "verbose", False))


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
        revision = identity.revision if with_qa else ""
        if not _apply_qa_enable(with_qa, revision):
            return None
        result = reconcile_bootstrap(
            intent,
            verbose=getattr(args, "verbose", False),
            qa_opt_out=without_qa,
            allow_qa_adoption=getattr(args, "force", False),
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


def _apply_qa_enable(with_qa: bool, revision: str) -> bool:
    if not with_qa:
        return True
    source = checkout_enclosing_cwd()
    if source is None:
        print(
            red(
                "--with-qa-skills must be run inside the complete primary "
                "Booley source checkout on main"
            )
        )
        return False
    try:
        enable_qa_skills(source, revision)
    except QaSkillSelectionError as exc:
        print(red(f"Cannot enable QA skills: {exc}"))
        return False
    return True


def _qa_prune_succeeded(result: BootstrapResult) -> bool:
    return any(
        finding.resource == "qa-skills"
        and finding.state in {BootstrapState.CURRENT, BootstrapState.CHANGED}
        for finding in result.findings
    )


def _render_result(result: BootstrapResult) -> int:
    print(bold_chrome("Host Bootstrap"))
    glyphs = {
        BootstrapState.CURRENT: (accent, "[--]"),
        BootstrapState.PENDING: (yellow, "[!!]"),
        BootstrapState.CHANGED: (green, "[OK]"),
        BootstrapState.WARNING: (yellow, "[!!]"),
        BootstrapState.ERROR: (red, "[XX]"),
    }
    for finding in result.findings:
        color, glyph = glyphs[finding.state]
        print(f"  {color(glyph)} {finding.resource}: {finding.detail}")
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
