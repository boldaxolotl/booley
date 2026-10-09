"""The managed line-ending rule shared by Initialization and committed-byte proofs."""

import os
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

GITATTRIBUTES_RULE = "* text=auto eol=lf"
AttributeQuery = Callable[..., subprocess.CompletedProcess[bytes]]


def _policy_lines(content: bytes) -> Iterator[bytes]:
    # Git attr.c uses this exact blank set; VT/FF are pattern bytes, not whitespace.
    for raw_line in content.split(b"\n"):
        line = raw_line.strip(b" \t\r\n")
        if line and not line.startswith(b"#"):
            yield line


def has_attribute_policy(content: bytes) -> bool:
    """Whether Git attribute lines contain anything beyond blanks and comments."""
    return any(_policy_lines(content))


def is_null_attributes_path(root: Path, path: Path) -> bool:
    """Match Git's null device with platform-native path case semantics."""
    return os.path.normcase(str(path)) == os.path.normcase(
        str(resolved_attribute_path(root, os.devnull))
    )


def has_managed_attributes(content: bytes, *, replayable: bool = False) -> bool:
    """Recognize the managed rule, optionally requiring init-compatible extra content."""
    lines = list(_policy_lines(content))
    rule = GITATTRIBUTES_RULE.encode()
    if not replayable:
        return rule in lines
    return lines.count(rule) == 1 and not local_policy_owned(
        b"\n".join(line for line in lines if line != rule)
    )


def local_policy_owned(content: bytes) -> bool:
    """Whether local policy prevents Initialization from prepending its default."""
    unrelated = {b"export-ignore", b"-export-ignore", b"export-subst", b"-export-subst"}
    for line in _policy_lines(content):
        fields = re.split(rb"[ \t\r\n]+", line)
        if len(fields) < 2 or any(field not in unrelated for field in fields[1:]):
            return True
    return False


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


def resolved_attribute_path(root: Path, value: str) -> Path | None:
    """Require Git-expanded attribute paths and resolve relative selections at the checkout."""
    if not value:
        return None
    if value.startswith(("~", "%(prefix)")):
        raise ValueError("Git did not expand the selected attributes path")
    path = Path(value)
    return path if path.is_absolute() else root / path


def native_attribute_path(
    root: Path, variable: str, query: AttributeQuery
) -> tuple[bool, Path | None]:
    """Git's selected attribute file, or unsupported discovery on legacy Git."""
    result = query(root, "var", variable)
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return True, None
    if result.returncode:
        diagnostic = os.fsdecode(result.stderr or b"") + os.fsdecode(result.stdout or b"")
        if "usage: git var" in diagnostic:
            return False, None
        raise ValueError(
            f"could not resolve {variable}: {os.fsdecode(result.stderr or b'').strip()}"
        )
    value = os.fsdecode(result.stdout or b"").removesuffix("\n")
    return True, resolved_attribute_path(root, value)


def configured_attributes_path(root: Path, query: AttributeQuery) -> tuple[bool, Path | None]:
    """The explicit user attribute selection, distinguishing empty selection from no config."""
    result = query(root, "config", "--path", "--get", "core.attributesFile")
    if result.returncode == 0:
        value = os.fsdecode(result.stdout or b"").removesuffix("\n")
        return True, resolved_attribute_path(root, value)
    if result.returncode != 1:
        raise ValueError(
            f"could not read core.attributesFile: {os.fsdecode(result.stderr or b'').strip()}"
        )
    return False, None


def fallback_user_attributes(root: Path, query: AttributeQuery) -> Path | None:
    """The same user-file selection when legacy Git cannot report GIT_ATTR_GLOBAL."""
    configured, selected = configured_attributes_path(root, query)
    if configured:
        return selected
    path = default_user_attributes()
    return resolved_attribute_path(root, str(path)) if path is not None else None
