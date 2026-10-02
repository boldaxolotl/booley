"""Flow-neutral classification of EDA design and execution failures."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.flows.base import SubprocessResult

FailureKind = Literal["missing_eda_tool", "missing_required_file", "missing_output"]
FailureOwner = Literal["design", "infrastructure"]

_STAGES = frozenset({"lint", "simulation", "sv2v", "yosys", "openroad", "vivado"})
_SUBJECT_RE = re.compile(r"[A-Za-z0-9_.+/-]+")
_MARKER_RE = re.compile(
    r"^BOOLEY_EDA_FAILURE token=(?P<token>[0-9a-f]{32}) "
    r"kind=(?P<kind>missing_eda_tool|missing_required_file|missing_output) "
    r"stage=(?P<stage>[a-z0-9_+-]+) subject=(?P<subject>[A-Za-z0-9_.+/-]+)$",
    re.MULTILINE,
)
_MISSING_EXE_PATTERNS = (
    re.compile(
        r"^(?:[\w./-]*(?:sh|bash|dash))(?::\s*(?:line\s+)?\d+)?:\s*"
        r"(?P<name>[\w.+-]+):\s*(?:command\s+)?not found",
        re.MULTILINE,
    ),
    re.compile(
        r"^make(?:\[\d+\])?:\s*(?P<name>[\w.+-]+):\s*"
        r"(?:command not found|no such file or directory)",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(r"^(?P<name>[\w.+-]+):\s*command not found", re.MULTILINE),
    re.compile(r"could not invoke\s+(?P<name>[\w.+-]+)\b", re.IGNORECASE),
    re.compile(r"No such file or directory:\s*'(?P<name>[\w.+-]+)'"),
)
_MISSING_REQUIRED_RE = re.compile(
    r"(?:fatal error|error):\s*(?P<name>[A-Za-z0-9_.+/-]+):\s*"
    r"No such file or directory",
    re.IGNORECASE,
)
_TOOLCHAIN_FILES = frozenset({"verilated.h", "verilated_vcd_c.h", "svdpi.h", "vpi_user.h"})


@dataclass(frozen=True)
class EdaFailure:
    """One classified failure with its owner and original diagnostic."""

    kind: FailureOwner
    failure_kind: FailureKind | Literal["abnormal_termination", "timeout", "design"]
    stage: str
    subject: str
    reason: str
    diagnostic: str


def new_attempt_token() -> str:
    """Return an unpredictable token authenticating one generated wrapper."""
    return secrets.token_hex(16)


def render_failure_marker(token: str, kind: FailureKind, stage: str, subject: str) -> str:
    """Render a validated, whitespace-free wrapper failure marker."""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("EDA failure token must be 32 lowercase hexadecimal characters")
    if stage not in _STAGES:
        raise ValueError(f"unsupported EDA failure stage: {stage}")
    if _SUBJECT_RE.fullmatch(subject) is None:
        raise ValueError(f"invalid EDA failure subject: {subject!r}")
    return f"BOOLEY_EDA_FAILURE token={token} kind={kind} stage={stage} subject={subject}"


def format_missing_eda_tool(executable: str) -> str:
    """Return the canonical actionable missing-program message."""
    name = Path(executable).name
    return (
        f"missing tool: {name}. Rebuild the Sandbox Image, then run "
        "`booley doctor` to verify EDA provisioning."
    )


def find_missing_executable(text: str) -> str | None:
    """Return the executable named by a recognized shell/process error."""
    for pattern in _MISSING_EXE_PATTERNS:
        match = pattern.search(text)
        if match:
            return Path(match.group("name")).name
    return None


def _authenticated_marker(
    text: str, expected_token: str | None, expected_stage: str | None
) -> tuple[FailureKind, str, str] | None:
    if expected_token is None:
        return None
    matches = [m for m in _MARKER_RE.finditer(text) if m["token"] == expected_token]
    if len(matches) != 1:
        return None
    marker = matches[0]
    stage = marker["stage"]
    if stage not in _STAGES or (expected_stage is not None and stage != expected_stage):
        return None
    return marker["kind"], stage, marker["subject"]


def _marker_failure(kind: FailureKind, stage: str, subject: str, text: str) -> EdaFailure:
    if kind == "missing_eda_tool":
        reason = format_missing_eda_tool(subject)
    elif kind == "missing_output":
        reason = f"{stage} did not produce required output: {subject}"
    else:
        reason = f"{stage} required dependency is unavailable: {subject}"
    return EdaFailure("infrastructure", kind, stage, subject, reason, text)


def _termination_failure(
    result: SubprocessResult,
    stage: str,
    text: str,
    boundary_executable: str | None,
) -> EdaFailure | None:
    """Classify terminal states that preclude any design verdict."""
    if result.timed_out:
        return EdaFailure("infrastructure", "timeout", stage, "", "EDA execution timed out", text)
    if result.oom_kill_delta <= 0 and 0 <= result.returncode < 128:
        return None
    if result.returncode == -1 and boundary_executable:
        subject = Path(boundary_executable).name
        return EdaFailure(
            "infrastructure",
            "missing_eda_tool",
            stage,
            subject,
            format_missing_eda_tool(subject),
            text,
        )
    return EdaFailure(
        "infrastructure",
        "abnormal_termination",
        stage,
        "",
        "EDA execution terminated abnormally",
        text,
    )


_LOADER_RE = re.compile(
    r"^(?P<name>[\w.+/-]+):\s*(?:error while loading shared libraries|"
    r"symbol lookup error):[^\r\n]*$",
    re.MULTILINE,
)
_STARTUP_RE = re.compile(
    r"^(?P<name>[\w.+/-]+):\s*(?:error|fatal):\s*(?:"
    r"failed to initialize|cannot initialize)[^\r\n]*$",
    re.MULTILINE | re.IGNORECASE,
)
_EDA_EXECUTABLES = frozenset(
    {
        "vivado",
        "sv2v",
        "yosys",
        "openroad",
        "verilator",
        "verilator_bin",
        "iverilog",
        "vvp",
        "verible-verilog-lint",
    }
)


def _startup_failure(
    text: str, executable: str | None, authenticated: bool, stage: str
) -> EdaFailure | None:
    owned = {Path(executable).name} if executable else set()
    if executable and Path(executable).name == "verilator":
        owned.add("verilator_bin")
    if authenticated:
        owned.update(_EDA_EXECUTABLES)
    for pattern in (_LOADER_RE, _STARTUP_RE):
        for match in pattern.finditer(text):
            name = Path(match["name"]).name
            if name in owned:
                diagnostic = match.group(0)[:500]
                return EdaFailure(
                    "infrastructure",
                    "missing_required_file",
                    stage,
                    name,
                    f"{name} startup failed: {diagnostic}",
                    diagnostic,
                )
    return None


def classify_eda_failure(
    result: SubprocessResult,
    *,
    expected_token: str | None = None,
    expected_stage: str | None = None,
    expected_executable: str | None = None,
    boundary_executable: str | None = None,
    authenticated_build: bool = False,
    design_diagnostic: str | None = None,
) -> EdaFailure | None:
    """Classify trusted execution evidence, with infrastructure precedence."""
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    marker = _authenticated_marker(text, expected_token, expected_stage)
    if marker is not None:
        return _marker_failure(*marker, text)
    stage = expected_stage or "simulation"
    termination = _termination_failure(
        result, stage, text, boundary_executable
    ) or _startup_failure(text, expected_executable, authenticated_build, stage)
    if termination is not None:
        return termination
    missing = find_missing_executable(text)
    trusted_missing = authenticated_build or (
        expected_executable is not None and missing == Path(expected_executable).name
    )
    if missing and trusted_missing:
        return EdaFailure(
            "infrastructure",
            "missing_eda_tool",
            stage,
            missing,
            format_missing_eda_tool(missing),
            text,
        )
    if authenticated_build:
        required = _MISSING_REQUIRED_RE.search(text)
        if required is not None:
            subject = Path(required["name"]).name
            if subject in _TOOLCHAIN_FILES or "/generated/" in required["name"]:
                return _marker_failure("missing_required_file", stage, subject, text)
    if design_diagnostic:
        return EdaFailure("design", "design", stage, "", design_diagnostic, design_diagnostic)
    return None


__all__ = [
    "EdaFailure",
    "FailureKind",
    "classify_eda_failure",
    "find_missing_executable",
    "format_missing_eda_tool",
    "new_attempt_token",
    "render_failure_marker",
]
