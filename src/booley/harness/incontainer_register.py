"""Compose Booley's in-container startup policy at the Harness entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from booley.runtime import incontainer_setup
from booley.runtime.project_dir import resolve_project_dir


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

        status = upgrade_review.observe(resolve_project_dir(project_root or Path.cwd()))
        if status.condition is not upgrade_review.ReviewCondition.CURRENT:
            print(f"warning: {upgrade_cli.render_status(status)}", file=sys.stderr)
        return status.condition.value
    except Exception:  # noqa: BLE001 -- post-start upgrade advice is fail-soft
        return "unavailable"


def main() -> None:
    """Start mechanisms, apply registration, and launch advisory Harness policy."""
    app = os.environ.get("BOOLEY_AGENT_APP", "none")
    server = "skipped" if app == "none" else incontainer_setup.ensure_http_server()
    status = incontainer_setup.register(app)
    upgrade = observe_upgrade()
    health = launch_auto_doctor()
    print(
        f"booley incontainer-register: server:{server} upgrade:{upgrade} health:{health} {status}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
