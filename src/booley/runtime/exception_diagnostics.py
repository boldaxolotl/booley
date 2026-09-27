"""Bounded user diagnoses and best-effort durable exception tracebacks."""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_EXCEPTION_MESSAGE_CHARS = 1_000
_MAX_DIAGNOSTIC_CHARS = 1_000_000


def bounded_exception_message(exc: Exception) -> str:
    """Return a single-line, bounded description without exposing ``repr`` payloads."""
    try:
        message = " ".join(str(exc).split())
    except Exception:
        logger.debug("Could not render exception message", exc_info=True)
        message = ""
    if not message:
        return "no additional detail"
    if len(message) <= _MAX_EXCEPTION_MESSAGE_CHARS:
        return message
    return f"{message[: _MAX_EXCEPTION_MESSAGE_CHARS - 3]}..."


def write_exception_diagnostic(
    exc: Exception,
    *,
    endpoint_name: str,
    invocation_id: str,
    report_dir: Path | None = None,
    transcript_path: Path | None = None,
    persist: bool = True,
) -> Path | None:
    """Persist a bounded traceback outside numbered invocation directories."""
    if not persist:
        return None
    path: Path | None = None
    try:
        if transcript_path is not None:
            path = transcript_path.with_suffix(".error.log")
        elif report_dir is not None:
            path = report_dir / ".endpoint-errors" / endpoint_name / f"{invocation_id}.log"
        else:
            return None
        diagnostic = "".join(traceback.format_exception(exc))
        if len(diagnostic) > _MAX_DIAGNOSTIC_CHARS:
            diagnostic = diagnostic[:_MAX_DIAGNOSTIC_CHARS] + "\n[traceback truncated]\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(diagnostic, encoding="utf-8")
    except Exception:
        logger.debug("Could not persist exception diagnostic to %s", path, exc_info=True)
        return None
    return path


def exception_report_text(
    endpoint_name: str,
    exc: Exception,
    diagnostic_path: Path | None = None,
) -> str:
    """Build the stable human-facing failure surface for an unexpected exception."""
    text = f"{endpoint_name} failed: {type(exc).__name__}: {bounded_exception_message(exc)}"
    if diagnostic_path is not None:
        text += f". Diagnostic: {diagnostic_path}"
    return text
