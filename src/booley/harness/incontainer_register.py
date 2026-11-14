"""Compose Booley's in-container startup policy at the Harness entry point."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from booley.config import agent, agent_app
from booley.config.gateway import (
    GATEWAY_AUTH,
    VISITOR_AUTH,
    GatewayConfigError,
    gateway_settings_from_env,
)
from booley.runtime import atomic_files, auth_token, health, incontainer_setup
from booley.runtime.booley_dir import resolve_booley_dir, resolve_common_booley_dir


def launch_auto_doctor(project_root: Path | None = None) -> str:
    """Start the stale-triggered health worker; registration never depends on it."""
    try:
        from booley.harness.auto_doctor import launch

        return launch(project_root or Path.cwd())
    except Exception:  # noqa: BLE001 -- post-start health is advisory
        return "failed"


def observe_upgrade(project_root: Path | None = None) -> str:
    """Observe the in-container package version without blocking registration."""
    try:
        from booley.harness import upgrade_cli, upgrade_review

        status = upgrade_review.observe(resolve_common_booley_dir(project_root or Path.cwd()))
        if status.condition is not upgrade_review.ReviewCondition.CURRENT:
            print(f"warning: {upgrade_cli.render_status(status)}", file=sys.stderr)
        return status.condition.value
    except Exception:  # noqa: BLE001 -- post-start upgrade advice is fail-soft
        return "unavailable"


def sandbox_trust_paths(start: Path) -> tuple[str, ...]:
    """Config keys of the Project Root and Booley Directory the agents may trust.

    The Project Root is *start*, the lifecycle hook's working directory: the
    workspace folder the Sandbox mounts, Git checkout or not. The Booley
    Directory comes from its own resolution, from the literal ``$BOOLEY_DIR``
    spelling, and from a ``.booley`` symlink. A resolution failure keeps the
    Project Root keys: registration must still run.
    """
    candidates = [start]
    try:
        candidates.append(resolve_booley_dir(start, cache_result=False))
    except Exception as exc:  # noqa: BLE001 -- trust for the Booley Directory is fail-soft
        print(f"warning: workspace trust for the Booley Directory skipped: {exc}", file=sys.stderr)
    if env_dir := os.environ.get("BOOLEY_DIR"):
        candidates.append(Path(env_dir))
    if (start / ".booley").is_symlink():
        candidates.append(start / ".booley")
    # Claude trusts everything below a trusted directory: never trust the
    # Project Root's own ancestors (a ``.booley`` link to ``/`` or the home).
    root = start.resolve()
    candidates = [path for path in candidates if path == start or not _is_ancestor(path, root)]
    return incontainer_setup.trust_keys(candidates)


def _is_ancestor(path: Path, descendant: Path) -> bool:
    """Whether *path* resolves to *descendant* itself or one of its parents."""
    return descendant.is_relative_to(path.resolve())


def _hook_start_directory() -> Path:
    """The hook's working directory, keeping a symlinked ``$PWD`` spelling."""
    cwd = Path.cwd()
    pwd = os.environ.get("PWD")
    if pwd and Path(pwd).is_absolute() and Path(pwd).resolve() == cwd.resolve():
        return Path(pwd)
    return cwd


_LABELS = {auth_token.APP_CLAUDE: "Claude", auth_token.APP_CODEX: "Codex"}


def resolve_auto_choice() -> agent_app.AgentAppChoice:
    """Pick the agent apps for ``BOOLEY_AGENT_APP=auto`` from this start's credentials.

    Every CLI with a credential is allowed, Claude first (the default), then
    Codex. With no credential: a complete gateway configuration (Codex alone),
    then Claude alone with its login offered. A visitor's own credential always
    beats the gateway, so ``BOOLEY_PRIMARY_AUTH=gateway`` counts as the
    visitor mode here: the gateway is only that mode's fallback. Reasons name credential sources
    (variables and paths), never values.
    """
    raw_policy = agent.env_auth_mode(os.environ.get("BOOLEY_PRIMARY_AUTH"))
    policy = None if raw_policy == GATEWAY_AUTH else raw_policy
    lookup = policy or VISITOR_AUTH
    # Look where registration looks ($HOME), so the published reason names a
    # credential the registered app will really use.
    home = incontainer_setup._agent_home()
    logged_in = {
        app: credential
        for app in _LABELS
        if (credential := auth_token.login_credential(app, lookup, home=home)) is not None
    }
    if logged_in:
        clis = tuple(logged_in)
        reason = "; ".join(
            f"{_LABELS[app]} credential from {credential.source}"
            for app, credential in logged_in.items()
        )
        if len(clis) > 1:
            reason += f"; {_LABELS[clis[0]]} is the default"
        return agent_app.AgentAppChoice(clis[0], policy, reason, clis)
    if lookup == VISITOR_AUTH:
        try:
            gateway = gateway_settings_from_env()
        except GatewayConfigError:
            pass
        else:
            return agent_app.AgentAppChoice(
                auth_token.APP_CODEX,
                GATEWAY_AUTH,
                f"no Claude or Codex credential; gateway at {gateway.url} ({gateway.token_source})",
                (auth_token.APP_CODEX,),
            )
    return agent_app.AgentAppChoice(
        auth_token.APP_CLAUDE,
        policy,
        "no credential and no gateway; Claude's login is offered",
        (auth_token.APP_CLAUDE,),
    )


def _read_record() -> bytes | None:
    """The published ``auto`` choice record's bytes, or ``None`` when unreadable."""
    try:
        return agent_app.record_path().read_bytes()
    except OSError:
        return None


def _publish_auto_choice() -> tuple[agent_app.AgentAppChoice | None, bool]:
    """Resolve and publish this start's ``auto`` choice.

    Returns the choice (``None`` unless ``auto``) and whether it differs from the
    record it replaced. The previous record goes first, so a failed resolution
    leaves none and consumers refuse instead of trusting a stale choice.
    """
    if not agent_app.auto_requested():
        return None, False
    previous = _read_record()
    atomic_files.durable_unlink(agent_app.record_path())
    choice = resolve_auto_choice()
    encoded = agent_app.encode(choice)
    atomic_files.atomic_replace_bytes(agent_app.record_path(), encoded)
    return choice, encoded != previous


def _invalidate_health() -> str:
    """Make automatic Doctor due after an agent-app change its fingerprint cannot see.

    Doctor grades the chosen app's credentials, so a stored result from before
    a sign-in can report a credential FAIL or WARN that no longer holds.
    """
    try:
        health.invalidate(Path.cwd(), reason="agent app changed")
    except Exception:  # noqa: BLE001 -- post-start health is advisory
        return " health-invalidation:failed"
    return ""


def _guarded_step(label: str, step: Callable[[], str]) -> str:
    """Run one registrar step: its ``label:status`` token, or ``refused`` on ``ValueError``."""
    try:
        return f"{label}:{step()}"
    except ValueError as exc:
        # An unreadable user config is refused untouched, like a refused registration.
        print(f"error: registration refused: {exc}", file=sys.stderr)
        return "refused"


def _register_clis(clis: tuple[str, ...], trust: tuple[str, ...]) -> str:
    """Register every allowed CLI; one CLI's refusal never stops the others.

    Returns the CLIs' status tokens, ``<cli>:refused`` for a refused one (an
    unreadable user config is left untouched), or ``none`` when no CLI is allowed.
    """
    if not clis:
        return incontainer_setup.register("none")
    statuses: list[str] = []
    for cli in clis:
        try:
            statuses.append(incontainer_setup.register(cli, trust_paths=trust))
        except ValueError as exc:
            # The rest of the hook (the other CLIs, upgrade note, auto-doctor) must
            # still run to help diagnose it.
            print(f"error: registration refused: {cli}: {exc}", file=sys.stderr)
            statuses.append(f"{cli}:refused")
    return " ".join(statuses)


def _hide_issued_gateway(status: str) -> str:
    """Append the step that blanks the gateway in Claude's settings to *status*.

    Guarded on its own, so a refused registration still hides the gateway. Codex's
    gateway provider needs no step here: an issued Sandbox lists Codex whenever its
    record routes it through the gateway, so its registration writes the provider.
    """
    return (
        f"{status} {_guarded_step('claude-gateway', incontainer_setup.hide_gateway_from_claude)}"
    )


def _any_refused(status: str) -> bool:
    """Whether a status line carries a refused registration or step."""
    return any(token == "refused" or token.endswith(":refused") for token in status.split())


def main() -> None:
    """Start mechanisms, apply registration, and launch advisory Harness policy."""
    try:
        choice, choice_changed = _publish_auto_choice()
    except agent_app.AgentAppError as exc:
        print(f"error: registration refused: {exc}", file=sys.stderr)
        sys.exit(1)
    # Right after the record changes, so no run that already sees the new app is
    # marked stale.
    invalidation = _invalidate_health() if choice_changed else ""
    clis = agent.sandbox_agent_clis()
    server = incontainer_setup.ensure_http_server(mode="interactive") if clis else "skipped"
    trust = sandbox_trust_paths(_hook_start_directory()) if clis else ()
    status = _hide_issued_gateway(_register_clis(clis, trust))
    upgrade = observe_upgrade()
    doctor_launch = launch_auto_doctor()
    print(
        f"booley incontainer-register: server:{server} upgrade:{upgrade} health:{doctor_launch} "
        f"{status}{_auto_suffix(choice)}{invalidation}",
        file=sys.stderr,
    )
    if _any_refused(status):
        sys.exit(1)


def _auto_suffix(choice: agent_app.AgentAppChoice | None) -> str:
    """The status line's note on the ``auto`` choice; empty for an explicit app."""
    if choice is None:
        return ""
    suffix = f" app:{choice.app} (BOOLEY_AGENT_APP=auto: {choice.reason})"
    if len(choice.clis) > 1:
        suffix += f" clis:{','.join(choice.clis)}"
    return suffix


if __name__ == "__main__":
    main()
