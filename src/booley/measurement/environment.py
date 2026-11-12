"""The scrubbed environment of a Flow that runs in an exported checkout.

A measurement must not depend on the shell that asked for it. The child gets an
allow-list of what the Sandbox itself issues; everything else the caller carries
(Python search paths, loader overrides, Git and Project selection, Goal bindings,
session routing, slot inheritance, credentials) is dropped by construction.

The checkout has no Git repository, so ``git`` is refused: the run's ``PATH``
starts with a directory holding a ``git`` that prints which command was refused
and exits 128 (Git's own "not a repository" status). A Flow therefore cannot
read the live working tree's history through Git. The refusal covers every
``git`` found through ``PATH``, also commands that need no repository
(``git --version``, ``git config --file``): none is on a Flow's path here.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from booley.runtime.compiler_cache import COMPILER_CACHE_ROOT_ENV
from booley.runtime.timefmt import LOCAL_TIMEZONE_ENV

# Spelled out: importing ``runtime.session_issuance`` for one name is far too heavy.
_SANDBOX_IMAGE_ID_ENV = "BOOLEY_SANDBOX_IMAGE_ID"

# Variables copied from the caller when present.
_ALLOWED_NAMES = frozenset(
    {
        "PATH",
        "LANG",
        "LANGUAGE",
        "TZ",
        "USER",
        "LOGNAME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        # The Sandbox marker and identity, and the image's build identity.
        "BOOLEY_CONTAINER",
        "BOOLEY_SANDBOX_FLAVOR",
        _SANDBOX_IMAGE_ID_ENV,
        "BOOLEY_EGRESS",
        "BOOLEY_VERSION",
        "BOOLEY_SOURCE_REVISION",
        "BOOLEY_SOURCE_UPDATED_AT",
        "BOOLEY_IMAGE_BUILT_AT",
        "BOOLEY_PAYLOAD_FINGERPRINT",
        "BOOLEY_BWAVE_BIN",
        "BOOLEY_RISCV_DOCS",
        LOCAL_TIMEZONE_ENV,
        COMPILER_CACHE_ROOT_ENV,
        # EDA tool licensing.
        "XILINXD_LICENSE_FILE",
        "LM_LICENSE_FILE",
    }
)
_ALLOWED_PREFIXES = ("LC_", "BOOLEY_FLEXNET_")

# One line naming the refused command; ``"$*"`` joins the arguments, reads no stdin.
_GIT_REFUSAL_SCRIPT = """#!/bin/sh
printf 'booley: git %s: the measurement checkout is not a git repository\\n' "$*" >&2
exit 128
"""


def write_git_refusal(directory: Path) -> None:
    """Create the executable ``git`` that refuses every call in *directory*."""
    script = directory / "git"
    script.write_bytes(_GIT_REFUSAL_SCRIPT.encode())
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def measurement_environment(
    source: Mapping[str, str],
    *,
    main_booley_dir: Path,
    home: Path,
    tmp: Path,
    git_refusal: Path,
) -> dict[str, str]:
    """Return the child environment: allow-listed *source* entries plus the run's own.

    ``BOOLEY_DIR`` names the Main Booley Directory, so shared state resolves there
    while the checkout's inputs resolve to its own Booley Directory. *home* and
    *tmp* are directories the caller created for this run, and
    ``PYTHONPYCACHEPREFIX`` keeps the bytecode of a Project Flow out of the checkout.
    *git_refusal* is the directory :func:`write_git_refusal` filled; it leads ``PATH``.
    """
    environment = {
        name: value
        for name, value in source.items()
        if name in _ALLOWED_NAMES or name.startswith(_ALLOWED_PREFIXES)
    }
    environment.update(
        {
            "BOOLEY_DIR": str(main_booley_dir),
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "PYTHONSAFEPATH": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPYCACHEPREFIX": str(tmp / "pycache"),
            "PATH": os.pathsep.join([str(git_refusal), source.get("PATH", os.defpath)]),
        }
    )
    return environment
