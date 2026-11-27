"""Container-side paths fixed by Session Issuance.

Leaf module with no Booley imports, so policy code (for example the
Simulation Flow's compiler cache) can name Sandbox locations without
importing the devcontainer renderer.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# ``.booley`` mounts here; BOOLEY_DIR (containerEnv) points at
# it so in-container tooling — including the Ticket-Mode Runner and its
# developer agent (ADR 0028) — resolves project config from one place.
BOOLEY_DIR_TARGET = "/booley"

WORK_DIR = "/work"
PROJECT_DATA_MOUNT_PATH = "/work/.booley"

# Read-only bind of the issued devcontainer spec; only Session Issuance mounts it.
DEVCONTAINER_TARGET = "/work/.devcontainer"

# The web Dashboard's loopback port in the Sandbox. The preview URL of T3 Code's locked
# button (``data/docker/t3code-policy.json``) names it.
DASHBOARD_PORT = 8090

# T3 Code's managed policy; Session Issuance mounts the Sandbox's own copy over it.
T3_POLICY_TARGET = "/etc/t3code/policy.json"
# T3 Code's base directory; the Project's T3 state volume is mounted there.
T3_BASE_DIR = "/home/agent/.t3"


def canonical_project_alias_path(path: Path) -> Path:
    """Translate only the protected image alias, preserving every suffix link.

    Ordinary directories and untrusted aliases retain their spelling so callers'
    nofollow guards still decide whether they are acceptable.
    """
    if os.name != "posix":
        return path
    alias = Path(BOOLEY_DIR_TARGET)
    if not path.is_absolute() or not path.is_relative_to(alias):
        return path
    try:
        info = alias.lstat()
        parent = alias.parent.lstat()
        trusted = (
            stat.S_ISLNK(info.st_mode)
            and info.st_uid == 0
            and stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == 0
            and not parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and str(alias.readlink()) == PROJECT_DATA_MOUNT_PATH
        )
    except OSError:
        return path
    if not trusted:
        return path
    if ".." in path.parts:
        raise ValueError("Project alias paths must not contain traversal")
    return Path(PROJECT_DATA_MOUNT_PATH) / path.relative_to(alias)
