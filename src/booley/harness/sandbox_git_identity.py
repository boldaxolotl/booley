"""Inspect whether Git inside a Sandbox commits with the ``[agent.git]`` identity.

Interactive Mode and Goal Mode supply the identity as command-scope Git
settings (``GIT_CONFIG_COUNT`` and friends). A hand-written devcontainer spec,
such as one for GitHub Codespaces, can omit them, leaving Git to fall back to a
copied host gitconfig or no identity at all. Rather than re-deriving Git's
precedence, these checks ask Git (``git config --show-scope``) what it would
use from the running process's environment.

The functions return data and print nothing; Doctor owns the presentation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.audit.doctor_waivers import DoctorWarning, warning
from booley.runtime.incontainer_git_identity import GitIdentityError, load_git_identity

OVERRIDE_CHECK = "sandbox.git-identity-override"
GITCONFIG_CHECK = "sandbox.gitconfig-identity"

_GIT_TIMEOUT_S = 10
_COMMAND_SCOPE = "command"
_FILE_SCOPES = ("system", "global")
_OVERRIDE_FIX = (
    "in a Booley-issued Sandbox, refresh and recreate the Sandbox so it picks up the "
    "current [agent.git]; in a hand-written devcontainer spec (for example GitHub "
    "Codespaces), set GIT_CONFIG_COUNT=2, GIT_CONFIG_KEY_0=user.name, "
    "GIT_CONFIG_VALUE_0=<name>, GIT_CONFIG_KEY_1=user.email, GIT_CONFIG_VALUE_1=<email> "
    "in both containerEnv and remoteEnv, then rebuild"
)
_GITCONFIG_FIX = (
    "while the override is complete this identity is only a fallback: waive "
    f"{GITCONFIG_CHECK} in doctor-waivers.toml. Booley's Sandbox Image sets a neutral "
    "Dev <dev@localhost> in /etc/gitconfig, and an editor may copy a host "
    "~/.gitconfig with user.*; remove the identity from the one named above to clear it"
)


@dataclass(frozen=True)
class IdentityCheck:
    """One check's outcome: a pass summary, or a warning plus its fix."""

    passed: str = ""
    warning: DoctorWarning | None = None
    fix: str = ""


@dataclass(frozen=True)
class _ConfigEntry:
    """One ``git config --show-scope --show-origin`` line."""

    scope: str
    origin: str
    key: str
    value: str


def _git_config(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git config`` in *checkout* with scope and origin columns."""
    return subprocess.run(
        ["git", "-C", str(checkout), "config", "--show-scope", "--show-origin", *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )


def _parse_entries(stdout: str, *, with_key: bool) -> list[_ConfigEntry]:
    """Parse ``scope<TAB>origin<TAB>[key<SPACE>]value`` lines."""
    entries = []
    for line in stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        scope, origin, rest = parts
        key, value = rest.split(" ", 1) if with_key and " " in rest else ("", rest)
        entries.append(_ConfigEntry(scope, origin, key, value))
    return entries


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else "no output"


def _override_problem(checkout: Path, key: str, want: str) -> str | None:
    """Describe how Git's effective *key* departs from *want*, or None when it matches."""
    result = _git_config(checkout, "--get", key)
    if result.returncode == 1:
        return f"{key} is not set by GIT_CONFIG_*; git has no value"
    if result.returncode != 0:
        return f"git cannot read {key}: {_first_line(result.stderr)}"
    entries = _parse_entries(result.stdout, with_key=False)
    if not entries:
        return f"git cannot read {key}: unparseable output"
    found = entries[-1]
    if found.scope != _COMMAND_SCOPE:
        return (
            f"{key} is not set by GIT_CONFIG_*; git uses '{found.value}' from "
            f"{found.scope} config ({found.origin})"
        )
    if found.value != want:
        return f"{key} override is '{found.value}', [agent.git] expects '{want}'"
    return None


def inspect_override(checkout: Path, booley_dir: Path) -> IdentityCheck:
    """Check that Git's effective identity is the ``[agent.git]`` command-scope override."""
    try:
        expected = load_git_identity(booley_dir)
    except GitIdentityError as exc:
        return IdentityCheck(
            warning=warning(OVERRIDE_CHECK, f"[agent.git] is invalid: {exc}"),
            fix="correct [agent.git] in booley.toml",
        )
    label = f"{expected.name} <{expected.email}>"
    try:
        problems = [
            problem
            for key, want in (("user.name", expected.name), ("user.email", expected.email))
            if (problem := _override_problem(checkout, key, want))
        ]
    except (OSError, subprocess.SubprocessError) as exc:
        problems = [f"Git could not be inspected: {exc}"]
    if not problems:
        return IdentityCheck(passed=f"Git identity override matches [agent.git] ({label})")
    message = (
        f"Git commits in this Sandbox will not use the [agent.git] identity {label}: "
        + "; ".join(problems)
    )
    return IdentityCheck(warning=warning(OVERRIDE_CHECK, message), fix=_OVERRIDE_FIX)


def inspect_gitconfig_identity(checkout: Path) -> IdentityCheck:
    """Check that no system or global gitconfig sets ``user.name`` / ``user.email``."""
    clean = IdentityCheck(passed="no user.name/user.email in system or global gitconfig")
    try:
        result = _git_config(checkout, "--get-regexp", r"^user\.(name|email)$")
    except (OSError, subprocess.SubprocessError) as exc:
        return _gitconfig_unreadable(f"Git could not be inspected: {exc}")
    if result.returncode == 1:
        return clean
    if result.returncode != 0:
        return _gitconfig_unreadable(_first_line(result.stderr))
    found = [
        entry
        for entry in _parse_entries(result.stdout, with_key=True)
        if entry.scope in _FILE_SCOPES
    ]
    if not found:
        return clean
    listing = ", ".join(f"{e.key}='{e.value}' ({e.origin})" for e in found)
    message = (
        f"system or global gitconfig sets {listing}; git uses it whenever the "
        "GIT_CONFIG_* override is absent or bypassed"
    )
    return IdentityCheck(warning=warning(GITCONFIG_CHECK, message), fix=_GITCONFIG_FIX)


def _gitconfig_unreadable(detail: str) -> IdentityCheck:
    return IdentityCheck(
        warning=warning(GITCONFIG_CHECK, f"cannot list system/global Git identity: {detail}"),
        fix="fix the Git configuration error above",
    )
