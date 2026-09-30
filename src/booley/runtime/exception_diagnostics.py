"""Bounded user diagnoses and best-effort durable exception tracebacks."""

from __future__ import annotations

import logging
import os
import re
import traceback
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

logger = logging.getLogger(__name__)

_MAX_EXCEPTION_MESSAGE_CHARS = 1_000
_MAX_DIAGNOSTIC_CHARS = 1_000_000
_MAX_PATH_COMPONENT_CHARS = 80


def bounded_message(message: str) -> str:
    """Return one bounded line of human-facing diagnostic text."""
    message = " ".join(message.split())
    if not message:
        return "no additional detail"
    if len(message) <= _MAX_EXCEPTION_MESSAGE_CHARS:
        return message
    return f"{message[: _MAX_EXCEPTION_MESSAGE_CHARS - 3]}..."


def bounded_exception_message(exc: Exception) -> str:
    """Return a single-line, bounded description without exposing ``repr`` payloads."""
    try:
        return bounded_message(str(exc))
    except Exception:
        logger.debug("Could not render exception message", exc_info=True)
        return "no additional detail"


def provider_exception_message(exc: Exception) -> str:
    """Prefer structured terminal provider text over a generic process summary."""
    result = getattr(exc, "result", None)
    if isinstance(result, str) and result.strip():
        return bounded_message(result)
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list):
        detail = "; ".join(item for item in errors if isinstance(item, str) and item.strip())
        if detail:
            return bounded_message(detail)
    return bounded_exception_message(exc)


def log_exception(
    target: logging.Logger,
    exc: Exception,
    *,
    summary: str,
) -> str:
    """Log one bounded ordinary summary plus one DEBUG traceback."""
    message = bounded_exception_message(exc)
    target.error("%s: %s", summary, message)
    target.debug("%s traceback", summary, exc_info=True)
    return message


def _safe_path_component(value: str) -> str:
    """Return one bounded component without allowing path traversal or collisions."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "unknown"
    if cleaned == value and len(cleaned) <= _MAX_PATH_COMPONENT_CHARS:
        return cleaned
    digest = sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    prefix = cleaned[: _MAX_PATH_COMPONENT_CHARS - len(digest) - 1]
    return f"{prefix}-{digest}"


def _atomic_write_text(path: Path, content: str) -> None:
    """Publish UTF-8 text atomically beside its final destination."""
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
        safe_invocation = _safe_path_component(invocation_id)
        diagnostic_id = f"{safe_invocation}-{uuid4().hex}"
        if transcript_path is not None:
            path = transcript_path.with_suffix(f".{diagnostic_id}.error.log")
        elif report_dir is not None:
            safe_endpoint = _safe_path_component(endpoint_name)
            path = report_dir / ".endpoint-errors" / safe_endpoint / f"{diagnostic_id}.log"
        else:
            return None
        diagnostic = "".join(traceback.format_exception(exc))
        if len(diagnostic) > _MAX_DIAGNOSTIC_CHARS:
            diagnostic = diagnostic[:_MAX_DIAGNOSTIC_CHARS] + "\n[traceback truncated]\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, diagnostic)
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
