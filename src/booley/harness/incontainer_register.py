"""Compose Booley's in-container startup policy at the Harness entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from booley.runtime import incontainer_setup
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
    return incontainer_setup.trust_keys(candidates)


def _hook_start_directory() -> Path:
    """The hook's working directory, keeping a symlinked ``$PWD`` spelling."""
    cwd = Path.cwd()
    pwd = os.environ.get("PWD")
    if pwd and Path(pwd).is_absolute() and Path(pwd).resolve() == cwd.resolve():
        return Path(pwd)
    return cwd


def main() -> None:
    """Start mechanisms, apply registration, and launch advisory Harness policy."""
    app = os.environ.get("BOOLEY_AGENT_APP", "none")
    server = (
        "skipped" if app == "none" else incontainer_setup.ensure_http_server(mode="interactive")
    )
    trust = () if app == "none" else sandbox_trust_paths(_hook_start_directory())
    status = incontainer_setup.register(app, trust_paths=trust)
    upgrade = observe_upgrade()
    health = launch_auto_doctor()
    print(
        f"booley incontainer-register: server:{server} upgrade:{upgrade} health:{health} {status}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
