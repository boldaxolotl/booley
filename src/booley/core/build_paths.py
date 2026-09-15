"""Checkout-local build-directory identity shared by Target inspection and Flows."""

from __future__ import annotations

import re
from pathlib import Path

from booley.core.checkout_role import require_project_checkout
from booley.core.project_dir import PROJECT_DIR_NAME

_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")
_EDALIZE_SUBDIR = Path(PROJECT_DIR_NAME) / ".runtime" / "edalize"


def work_root_for(
    work_dir: Path | str,
    flow: str,
    config: str,
    *,
    variant: str = "",
) -> Path:
    """Return the canonical per-(Flow, config[, variant]) build directory.

    Preserve the checkout-local layout independently of ambient Project-directory
    overrides so different worktrees cannot share mutable build caches. This only
    calculates a path; callers own directory creation, leasing and execution.
    """
    root = require_project_checkout(Path(work_dir))
    safe = _NAME_SANITIZE_RE.sub("_", config).strip("_") or "config"
    leaf = f"{safe}-{variant}" if variant else safe
    return root / _EDALIZE_SUBDIR / flow / leaf
