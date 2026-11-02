"""Rebuild generated Target inputs in a baseline checkout with the Project's post-setup hook.

A Goal worktree gets its ignored build inputs (a firmware image, a copied
simulator main) from ``.booley/hooks/post-setup``, run before Goal entry. A
fresh baseline checkout has none of them. Running the same hook there rebuilds
them from base sources. The hook is a Protected Input restored from the entry
capture, so its outputs are a function of the base commit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from booley.flows.execution import flow_enabled
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.pinned_history import raw_git
from booley.runtime.project_prepare import prepare_project

#: Proof label for a dependency that the post-setup hook wrote in the baseline checkout.
HOOK_PROOF = "post-setup-hook"
#: The hook's share of a baseline Job's budget; a hung hook must leave time to measure.
HOOK_TIMEOUT_S = 900


def run_post_setup_hook(checkout: Path, *, slug: str, timeout_s: float) -> dict[str, Any]:
    """Run the hook in *checkout*; report the untracked files it wrote and any failure.

    Returns ``{"files": {name: sha256}, "error": str}``. A failed hook is not
    fatal here: only a dependency that needed its output becomes unavailable.
    """
    before = untracked_digests(checkout)
    result = prepare_project(
        checkout,
        checkout,
        slug=slug,
        sim_flow_enabled=flow_enabled("sim", checkout),
        timeout_s=max(1, int(min(HOOK_TIMEOUT_S, timeout_s))),
    )
    if not result.ok or result.hook is None:
        return {"files": {}, "error": result.error}
    after = untracked_digests(checkout)
    written = {name: digest for name, digest in after.items() if before.get(name) != digest}
    return {"files": written, "error": ""}


def untracked_digests(checkout: Path) -> dict[str, str]:
    """SHA-256 of every untracked or ignored regular file, keyed checkout-relative.

    Covers the checkout's repository and a paired Booley Directory repository
    inside it. Git lists a nested repository as one ``dir/`` entry, so its
    files are never attributed to the outer repository.
    """
    repositories = [checkout]
    booley_dir = resolve_checkout_booley_dir(checkout)
    if booley_dir.is_relative_to(checkout) and (booley_dir / ".git").exists():
        repositories.append(booley_dir)
    digests: dict[str, str] = {}
    for repository in repositories:
        listing = raw_git(repository, "ls-files", "-z", "--others")
        for raw in listing.split(b"\0"):
            if not raw or raw.endswith(b"/"):
                continue
            path = repository / raw.decode("utf-8", "surrogateescape")
            digest = file_digest(path)
            if digest is not None:
                digests[path.relative_to(checkout).as_posix()] = digest
    return digests


def file_digest(path: Path) -> str | None:
    """SHA-256 of a regular, non-symlink file; ``None`` for anything else."""
    if path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
