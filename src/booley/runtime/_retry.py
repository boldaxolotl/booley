"""Retry and resilience primitives shared across agent backends."""

from __future__ import annotations

import random
import re
from pathlib import Path

# --- API resilience constants (owned here, re-exported by config.py) ---
# Exponential backoff with jitter: base 30s → 60s → 120s → 240s → 300s,
# each randomized by ±JITTER_FRACTION to avoid lockstep retries.
MAX_API_RETRIES = 5
API_RETRY_INITIAL_BACKOFF_S = 30
API_RETRY_MAX_BACKOFF_S = 300
API_RETRY_BACKOFF_MULTIPLIER = 2.0
API_RETRY_JITTER_FRACTION = 0.25
RATE_LIMIT_SLEEP_BUFFER_S = 60
RATE_LIMIT_FALLBACK_BACKOFF_S = 300

# Provider HTTP statuses count only as whole tokens, so "1500 bytes",
# "max_tokens 2500", "500.5" and a request ID like "req_015003" do not match.
_TRANSIENT_STATUS_RE = re.compile(r"(?<![\w-])(?<!\d\.)(?:500|502|503|529)(?![\w-])(?!\.\d)")
_TRANSIENT_PHRASES = (
    "internal server error",
    "bad gateway",
    "service unavailable",
    "overloaded",
    "econnreset",
    "etimedout",
    # Codex mid-stream drop after its own reconnect ladder is exhausted —
    # transient once the proxy env is in place (mcp_config forwards it).
    "stream disconnected",
)
# A failed connection, not any text that mentions one ("Invalid connection string").
_CONNECTION_FAILURE_RE = re.compile(
    r"\bconnection (?:was )?(?:forcibly )?"
    r"(?:reset|refused|closed|aborted|timed out|error|lost|terminated|failed)\b"
    r"|\bclosed connection\b|\bfailed to establish a new connection\b"
    r"|\bconnection(?:reset|refused|aborted)?error\b|\bconnection(?:reset|refused|aborted)"
)
# The Claude SDK's ProcessError text when the CLI exits nonzero and reports no
# provider message: the cause is unknown, so it gets the transient retry budget.
_BARE_CLI_EXIT_PREFIX = "command failed with exit code"


def _is_bare_cli_exit(text: str) -> bool:
    """Return whether lowercased *text* is a nonzero CLI exit with no provider message."""
    return text.startswith(_BARE_CLI_EXIT_PREFIX)


def _is_transient_error(exc: Exception) -> bool:
    """Check if an exception represents a transient API/network error."""
    text = str(exc).lower()
    return (
        _TRANSIENT_STATUS_RE.search(text) is not None
        or any(phrase in text for phrase in _TRANSIENT_PHRASES)
        or _CONNECTION_FAILURE_RE.search(text) is not None
        or _is_bare_cli_exit(text)
    )


def compute_backoff(attempt: int, retry_after: float | None = None) -> float:
    """Calculate backoff duration for a retry attempt.

    If retry_after is provided and > 0, uses it directly.
    If retry_after is None, computes exponential backoff with jitter.
    If retry_after is 0, returns 0.
    """
    if retry_after is not None and retry_after > 0:
        return retry_after
    if retry_after is not None:
        return 0.0
    base = min(
        API_RETRY_INITIAL_BACKOFF_S * (API_RETRY_BACKOFF_MULTIPLIER ** (attempt - 1)),
        API_RETRY_MAX_BACKOFF_S,
    )
    jitter_span = base * API_RETRY_JITTER_FRACTION
    return base + random.uniform(-jitter_span, jitter_span)


def transcript_path_for_attempt(base_path: Path | None, attempt: int) -> Path | None:
    """Return transcript path with -retryN suffix for retries."""
    if base_path is None:
        return None
    if attempt <= 1:
        return base_path
    return base_path.with_name(f"{base_path.stem}-retry{attempt}{base_path.suffix}")


# Run-indexed transcript stems produced by developer crash-recovery rotation
# (developer.py::_detect_crash_recovery): run_NNN, optionally with -retryN.
_RUN_INDEXED_STEM_RE = re.compile(r"^(?P<run>run_\d+)(?P<retry>-retry\d+)?$")


def transcript_path_for_label(base_path: Path | None, label: str | None) -> Path | None:
    """Derive per-invocation transcript path from label.

    Multi-invocation specialists (e.g. reviewer's per-category labels)
    pass different labels so their
    transcripts don't overwrite each other.
    Preserves any -retryN suffix already present in the base path.

    Run-indexed base paths (``run_NNN[-retryN].jsonl``) keep their run tag so
    crash-recovery rotation survives relabeling: ``run_003.jsonl`` + label
    ``developer`` -> ``developer.run_003.jsonl`` (previously the label replaced
    the stem, so every run overwrote ``developer.jsonl``).
    """
    if base_path is None or not label:
        return base_path
    stem = base_path.stem
    run_match = _RUN_INDEXED_STEM_RE.match(stem)
    if run_match:
        run_tag = run_match.group("run")
        retry_tag = run_match.group("retry") or ""
        return base_path.parent / f"{label}.{run_tag}{retry_tag}{base_path.suffix}"
    retry_tag = ""
    if "-retry" in stem:
        retry_tag = stem[stem.index("-retry") :]
    return base_path.parent / f"{label}{retry_tag}{base_path.suffix}"
