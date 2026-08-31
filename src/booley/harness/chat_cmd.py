"""Launch the agent CLI selected by the current Project."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from booley.config.settings import BackendConfigError, get_backend_config, load_models_config
from booley.runtime.project_dir import resolve_checkout_project_dir


def run(_args: argparse.Namespace, project_root: Path) -> int:
    """Replace Booley with the Project's configured interactive agent CLI."""
    try:
        project_dir = resolve_checkout_project_dir(project_root)
        load_models_config(project_root, project_dir=project_dir)
        provider = get_backend_config().provider
    except (BackendConfigError, FileNotFoundError) as exc:
        print(f"ERROR: could not resolve the Project's agent provider: {exc}", file=sys.stderr)
        return 2

    try:
        os.execvp(provider, [provider])
    except FileNotFoundError:
        print(
            f"ERROR: configured agent CLI '{provider}' was not found on PATH.",
            file=sys.stderr,
        )
    except OSError as exc:
        print(f"ERROR: could not launch agent CLI '{provider}': {exc}", file=sys.stderr)
    return 2
