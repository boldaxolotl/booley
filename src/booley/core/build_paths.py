"""Checkout-local build-directory identity shared by Target inspection and Flows."""

from __future__ import annotations

import re
from pathlib import Path

from booley.core.booley_dir import in_checkout_booley_dir
from booley.core.checkout_role import require_project_checkout

_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")


def work_root_for(
    work_dir: Path | str,
    flow: str,
    config: str,
    *,
    variant: str = "",
) -> Path:
    """Return the canonical per-(Flow, config[, variant]) build directory.

    Live under the checkout's own Booley Directory (its root ``booley.toml``
    ``[project].dir``, else ``.booley``), independently of ambient Booley Directory
    overrides, so different worktrees cannot share mutable build caches. This only
    calculates a path; callers own directory creation, leasing and execution.
    """
    root = require_project_checkout(Path(work_dir))
    safe = _NAME_SANITIZE_RE.sub("_", config).strip("_") or "config"
    leaf = f"{safe}-{variant}" if variant else safe
    return in_checkout_booley_dir(root) / ".runtime" / "edalize" / flow / leaf
