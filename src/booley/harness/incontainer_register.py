"""Compose Booley's in-container startup policy at the Harness entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from booley.config import agent_app
from booley.config.gateway import GATEWAY_AUTH, GatewayConfigError, gateway_settings_from_env
from booley.runtime import atomic_files, auth_token, incontainer_setup
from booley.runtime.booley_dir import resolve_booley_dir


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

        status = upgrade_review.observe(resolve_booley_dir(project_root or Path.cwd()))
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


def resolve_auto_choice() -> agent_app.AgentAppChoice:
    """Pick the agent app for ``BOOLEY_AGENT_APP=auto`` from this start's credentials.

    Order: a Claude credential, a Codex credential, a complete gateway
    configuration (Codex), then Claude with its login offered. A visitor's own
    credential always beats the gateway, so ``BOOLEY_PRIMARY_AUTH=gateway``
    counts as ``auto`` here: the gateway is only the fallback. Reasons name
    credential sources (variables and paths), never values.
    """
    raw_policy = (os.environ.get("BOOLEY_PRIMARY_AUTH") or "").strip().lower() or None
    policy = None if raw_policy == GATEWAY_AUTH else raw_policy
    lookup = policy or "auto"
    claude = auth_token.effective_credential(auth_token.APP_CLAUDE, lookup)
    codex = auth_token.effective_credential(auth_token.APP_CODEX, lookup)
    if claude is not None:
        reason = f"Claude credential from {claude.source}"
        if codex is not None:
            reason += "; a Codex credential is also present, Claude is preferred"
        return agent_app.AgentAppChoice(auth_token.APP_CLAUDE, policy, reason)
    if codex is not None:
        return agent_app.AgentAppChoice(
            auth_token.APP_CODEX, policy, f"Codex credential from {codex.source}"
        )
    if lookup == "auto":
        try:
            gateway = gateway_settings_from_env()
        except GatewayConfigError:
            pass
        else:
            return agent_app.AgentAppChoice(
                auth_token.APP_CODEX,
                GATEWAY_AUTH,
                f"no Claude or Codex credential; gateway at {gateway.url} ({gateway.token_source})",
            )
    return agent_app.AgentAppChoice(
        auth_token.APP_CLAUDE, policy, "no credential and no gateway; Claude's login is offered"
    )


def _publish_auto_choice() -> agent_app.AgentAppChoice | None:
    """Resolve and publish this start's ``auto`` choice; ``None`` unless ``auto``.

    The previous start's record goes first, so a failed resolution leaves none
    and consumers refuse instead of trusting a stale choice.
    """
    if not agent_app.auto_requested():
        return None
    atomic_files.durable_unlink(agent_app.record_path())
    choice = resolve_auto_choice()
    atomic_files.atomic_replace_bytes(agent_app.record_path(), agent_app.encode(choice))
    return choice


def main() -> None:
    """Start mechanisms, apply registration, and launch advisory Harness policy."""
    try:
        choice = _publish_auto_choice()
    except agent_app.AgentAppError as exc:
        print(f"error: registration refused: {exc}", file=sys.stderr)
        sys.exit(1)
    app = choice.app if choice else os.environ.get("BOOLEY_AGENT_APP", "none")
    server = (
        "skipped" if app == "none" else incontainer_setup.ensure_http_server(mode="interactive")
    )
    trust = () if app == "none" else sandbox_trust_paths(_hook_start_directory())
    try:
        status = incontainer_setup.register(app, trust_paths=trust)
    except ValueError as exc:
        # An unreadable user config is refused untouched; the rest of the hook
        # (upgrade note, auto-doctor) must still run to help diagnose it.
        print(f"error: registration refused: {exc}", file=sys.stderr)
        status = "refused"
    upgrade = observe_upgrade()
    health = launch_auto_doctor()
    chosen = f" app:{choice.app} (BOOLEY_AGENT_APP=auto: {choice.reason})" if choice else ""
    print(
        f"booley incontainer-register: server:{server} upgrade:{upgrade} health:{health} "
        f"{status}{chosen}",
        file=sys.stderr,
    )
    if status == "refused":
        sys.exit(1)


if __name__ == "__main__":
    main()
