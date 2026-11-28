"""Inspect whether Git inside a Sandbox commits with the ``[agent.git]`` identity.

The Sandbox supplies the identity as command-scope Git
settings (``GIT_CONFIG_COUNT`` and friends). A hand-written devcontainer spec,
such as one for GitHub Codespaces, can omit them, leaving Git to fall back to a
copied host gitconfig or no identity at all. Rather than re-deriving Git's
precedence, these checks ask Git (``git config --show-scope``) what it would
use from the running process's environment.

The module also audits system and global credential helpers, which an editor
attach installs to forward host Git credentials into the Sandbox.

The functions return data and print nothing; Doctor owns the presentation.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.audit.doctor_waivers import DoctorWarning, warning
from booley.runtime.incontainer_git_identity import (
    DEFAULT_IDENTITY,
    GitIdentityError,
    load_git_identity,
)
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS

OVERRIDE_CHECK = "sandbox.git-identity-override"
GITCONFIG_CHECK = "sandbox.gitconfig-identity"
CREDENTIAL_HELPER_CHECK = "sandbox.git-credential-helper"

_GIT_TIMEOUT_S = 10
_COMMAND_SCOPE = "command"
_FILE_SCOPES = ("system", "global")
_OVERRIDE_FIX = (
    "in a Booley-issued Sandbox, recreate the Sandbox so it picks up the "
    f"current [agent.git] ({UNKNOWN_SANDBOX_STEPS}); in a hand-written devcontainer spec (for example GitHub "
    "Codespaces), set GIT_CONFIG_COUNT=2, GIT_CONFIG_KEY_0=user.name, "
    "GIT_CONFIG_VALUE_0=<name>, GIT_CONFIG_KEY_1=user.email, GIT_CONFIG_VALUE_1=<email> "
    "in both containerEnv and remoteEnv, then rebuild"
)
_GITCONFIG_FIX = (
    "while the override is complete this identity is only a fallback: waive "
    f"{GITCONFIG_CHECK} in doctor-waivers.toml, or remove the user.name/user.email "
    "entries from the gitconfig named above"
)
_CREDENTIAL_HELPER_FIX = (
    "remove this credential helper line from the gitconfig named above; to stop "
    'VS Code adding it again, set dev.containers.gitCredentialHelperConfigLocation to "none" '
    "and dev.containers.copyGitConfig to false in your host VS Code settings; or, to accept "
    f"this helper, waive {CREDENTIAL_HELPER_CHECK} with this warning's subject in "
    "doctor-waivers.toml"
)
_USERINFO_RE = re.compile(r"(://)[^/\s]*@")
_QUOTED_SECRET_RE = re.compile(r"""(['"])(password|username)=[^'"]*\1""")
_INLINE_SECRET_RE = re.compile(r"\b(password|username)=\S+")
_NEUTRAL_VALUES = {"user.name": DEFAULT_IDENTITY.name, "user.email": DEFAULT_IDENTITY.email}


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
        ["git", "-C", str(checkout), "config", "-z", "--show-scope", "--show-origin", *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )


def _parse_entries(stdout: str, *, with_key: bool) -> list[_ConfigEntry]:
    """Parse NUL-delimited ``scope, origin, [key<NEWLINE>]value`` triples (``-z`` output)."""
    fields = stdout.split("\0")
    entries = []
    for scope, origin, rest in zip(fields[0::3], fields[1::3], fields[2::3], strict=False):
        if not with_key:
            key, value = "", rest
        else:
            key, _, value = rest.partition("\n")
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
    """Check that no system or global gitconfig sets a foreign ``user.name`` / ``user.email``.

    The Sandbox Image's own neutral ``Dev <dev@localhost>`` is not foreign, per value.
    """
    clean = IdentityCheck(
        passed=(
            "system and global gitconfig set no identity other than "
            f"{DEFAULT_IDENTITY.name} <{DEFAULT_IDENTITY.email}>"
        )
    )
    try:
        result = _git_config(checkout, "--get-regexp", r"^user\.(name|email)$")
    except (OSError, subprocess.SubprocessError) as exc:
        return _gitconfig_unreadable(
            GITCONFIG_CHECK, "identity", f"Git could not be inspected: {exc}"
        )
    if result.returncode == 1:
        return clean
    if result.returncode != 0:
        return _gitconfig_unreadable(GITCONFIG_CHECK, "identity", _first_line(result.stderr))
    found = [
        entry
        for entry in _parse_entries(result.stdout, with_key=True)
        if entry.scope in _FILE_SCOPES and entry.value != _NEUTRAL_VALUES.get(entry.key)
    ]
    if not found:
        return clean
    listing = ", ".join(f"{e.key}='{e.value}' ({e.origin})" for e in found)
    message = (
        f"system or global gitconfig sets {listing}; git uses it whenever the "
        "GIT_CONFIG_* override is absent or bypassed"
    )
    return IdentityCheck(warning=warning(GITCONFIG_CHECK, message), fix=_GITCONFIG_FIX)


def _redacted(text: str) -> str:
    """Drop URL userinfo and inline ``password=``/``username=`` values; collapse whitespace."""
    text = _USERINFO_RE.sub(r"\1***@", text)
    text = _QUOTED_SECRET_RE.sub(r"\1\2=***\1", text)
    text = _INLINE_SECRET_RE.sub(r"\1=***", text)
    return " ".join(text.split())


def inspect_credential_helpers(checkout: Path) -> list[IdentityCheck]:
    """Check that no system or global gitconfig sets a Git credential helper.

    Returns one warning per helper, whose subject is the redacted ``<key>=<value>``
    so a waiver can accept that helper; otherwise a single pass or inspection failure.
    Only lists configuration; no helper and no ``git credential`` command runs.
    """
    clean = IdentityCheck(passed="system and global gitconfig set no Git credential helper")
    try:
        result = _git_config(checkout, "--get-regexp", r"^credential\.(.+\.)?helper$")
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            _gitconfig_unreadable(
                CREDENTIAL_HELPER_CHECK, "credential helpers", f"Git could not be inspected: {exc}"
            )
        ]
    if result.returncode == 1:
        return [clean]
    if result.returncode != 0:
        return [
            _gitconfig_unreadable(
                CREDENTIAL_HELPER_CHECK, "credential helpers", _first_line(result.stderr)
            )
        ]
    found: list[_ConfigEntry] = []
    for entry in _parse_entries(result.stdout, with_key=True):
        if entry.scope not in _FILE_SCOPES:
            continue
        if not entry.value:  # Git: an empty value resets the helpers listed before it
            found = [prior for prior in found if prior.key != entry.key]
        else:
            found.append(entry)
    if not found:
        return [clean]
    return [_credential_helper_check(entry, position) for position, entry in enumerate(found)]


def _credential_helper_check(entry: _ConfigEntry, position: int) -> IdentityCheck:
    """Warn about one helper; *position* keeps helpers that redact identically apart."""
    key, value = _redacted(entry.key), _redacted(entry.value)
    message = (
        f"system or global gitconfig sets Git credential helper {key}='{value}' "
        f"({entry.origin}); any process in this Sandbox, including the agent, can ask it "
        "for credentials. An editor attach (VS Code Dev Containers, Codespaces) likely "
        "installed it; the Sandbox Image sets none"
    )
    return IdentityCheck(
        warning=warning(
            CREDENTIAL_HELPER_CHECK, message, subject=f"{key}={value}", dedupe=str(position)
        ),
        fix=_CREDENTIAL_HELPER_FIX,
    )


def _gitconfig_unreadable(check_id: str, what: str, detail: str) -> IdentityCheck:
    return IdentityCheck(
        warning=warning(check_id, f"cannot list system/global Git {what}: {detail}"),
        fix="fix the Git configuration error above",
    )
