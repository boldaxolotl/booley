"""The host workflows that recreate a Sandbox, worded once.

``docs/user/SANDBOX.md`` ("Applying Sandbox changes") is the authority. Every
remedy that tells a user to recreate a Sandbox builds its text here, so the
documented steps and the printed steps cannot drift apart.
"""

from __future__ import annotations

import os
import shlex
from enum import StrEnum
from pathlib import Path

from booley.core.booley_dir_pin import BooleyDirPinError
from booley.core.host_shell import quote_arg
from booley.runtime.docs_links import docs_url

__all__ = [
    "APPLY_DOC_URL",
    "REBUILD_CONTAINER",
    "UNKNOWN_SANDBOX_STEPS",
    "Attachment",
    "apply_steps",
    "image_is_managed",
    "shell_command",
]


class Attachment(StrEnum):
    """How a Sandbox is entered: VS Code Dev Containers or ``booley session``."""

    VSCODE = "vscode"
    HEADLESS = "headless"


REBUILD_CONTAINER = "use Dev Containers: Rebuild Container"
APPLY_DOC_URL = docs_url("SANDBOX.md", "applying-sandbox-changes")


def shell_command(argv: list[str]) -> str:
    """Quote *argv* for the host shell: POSIX ``shlex.join``, PowerShell on Windows."""
    if os.name == "nt":
        return "& " + " ".join(quote_arg(arg) for arg in argv)
    return shlex.join(argv)


def image_is_managed(project_root: Path) -> bool:
    """Whether Booley builds the Project's Sandbox Image (the rule refresh uses).

    An invalid ``[sandbox].image`` or an unpinned ``.booley`` link counts as
    managed: the remedy is advice, and ``booley init`` reports either itself.
    """
    from booley.runtime import image_lifecycle

    try:
        return not image_lifecycle.is_externally_managed(project_root)
    except (image_lifecycle.ImageLifecycleError, BooleyDirPinError):
        return True


def _command(words: str, project: str | None) -> str:
    """Return one backticked ``booley`` command; only the Project Root needs quoting."""
    scope = f" --project {quote_arg(project)}" if project is not None else ""
    return f"`booley {words}{scope}`"


def _vscode(managed: bool, project: str | None) -> str:
    init = _command("init" if managed else "init --seed", project)
    return f"on the host run {init}, then {REBUILD_CONTAINER}"


def _headless(managed: bool, refreshable: bool, project: str | None) -> str:
    if managed and refreshable:
        return f"run {_command('session refresh', project)} on the host"
    down = _command("session down", project)
    seed = _command("init --seed", project)
    up = _command("session up", project)
    return f"run {down}, {seed}, and {up} on the host"


def apply_steps(
    attachment: Attachment | None,
    *,
    managed: bool,
    refreshable: bool = True,
    project: str | None = None,
) -> str:
    """Return the documented steps for one Sandbox owner and image kind.

    ``refreshable=False`` (an unusable issuance stamp) sends a headless Sandbox
    through the reseed steps, because ``session refresh`` cannot use that stamp.
    ``project`` adds ``--project <root>`` to every ``booley`` command.
    ``None`` is an unknown owner and lists both workflows.
    """
    if attachment is Attachment.VSCODE:
        return _vscode(managed, project)
    if attachment is Attachment.HEADLESS:
        return _headless(managed, refreshable, project)
    return (
        f"in VS Code, {_vscode(managed, project)}; "
        f"headless, {_headless(managed, refreshable, project)}"
    )


UNKNOWN_SANDBOX_STEPS = (
    f"{apply_steps(None, managed=True)}; with your own image, follow the steps in {APPLY_DOC_URL}"
)
