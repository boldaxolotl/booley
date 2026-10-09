"""The managed line-ending rule shared by Initialization and committed-byte proofs."""

import os
from pathlib import Path

GITATTRIBUTES_RULE = "* text=auto eol=lf"


def has_managed_attributes(content: bytes, *, exclusive: bool = False) -> bool:
    """Recognize the managed rule, optionally requiring it to be the only line."""
    lines = [line.strip() for line in content.splitlines()]
    rule = GITATTRIBUTES_RULE.encode()
    return lines == [rule] if exclusive else rule in lines


def system_attributes_disabled() -> bool:
    """Interpret Git's environment switch before inspecting its system policy."""
    value = os.environ.get("GIT_ATTR_NOSYSTEM", "").lower()
    if value in ("", "0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    try:
        return int(value) != 0
    except ValueError as exc:
        raise ValueError("GIT_ATTR_NOSYSTEM is not a Git Boolean") from exc


def default_user_attributes() -> Path | None:
    """Git's default user policy when older Git cannot report its selected path."""
    xdg, home = os.environ.get("XDG_CONFIG_HOME"), os.environ.get("HOME")
    if xdg:
        return Path(xdg) / "git/attributes"
    return Path(home) / ".config/git/attributes" if home else None
