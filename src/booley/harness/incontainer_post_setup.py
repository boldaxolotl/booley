"""In-Sandbox post-setup of the main checkout: ``python -m booley.harness.incontainer_post_setup``.

The host runs it through the hook shell, whose working directory is the main checkout,
before the lifecycle commands that launch the Automatic Doctor.
"""

from __future__ import annotations

import sys
from pathlib import Path

from booley.harness import worktree_cmd
from booley.runtime.booley_dir import resolve_checkout_booley_dir

# Names the log beside those of the prepared worktrees.
MAIN_CHECKOUT_LOG = "main-checkout"


def prepare_main_checkout(main: Path) -> int:
    """Run the Project's post-setup hook in *main*; a failed hook only warns, so return 0."""
    try:
        # Not ``<main>/.booley``: creating that would shadow an external Booley Directory.
        worktree_cmd.run_post_setup(resolve_checkout_booley_dir(main), main, MAIN_CHECKOUT_LOG)
    except worktree_cmd.WorktreePostSetupError as exc:
        print(f"warning: {exc}; the Sandbox starts anyway", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(prepare_main_checkout(Path.cwd()))
