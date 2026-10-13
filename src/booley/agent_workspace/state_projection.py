"""Category projection of development state, including restoration on exit."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RTL_SIDE_PREFIXES: tuple[str, ...] = (
    "review_rtl_",
    "lint_",
    "synthesis_",
    "fpga_impl_",
    "elaborate_",
)

# Criterion-key prefixes that describe TB-side artifacts.
_TB_SIDE_PREFIXES: tuple[str, ...] = ("review_tb_",)

# ``detail`` keys that carry only summary/gating info (safe to expose across
# the boundary). Anything not in this set is stripped from opposite-category
# criteria — better to be over-aggressive than to ship a new leak channel.
_DETAIL_SUMMARY_KEYS: frozenset[str] = frozenset(
    {
        "CRITICAL",
        "MAJOR",
        "MINOR",
        "issues",
        "original_issues",
        "verify_attempts",
        "total_verify_cycles",
        "elapsed_s",
        "warnings",
    }
)


def _is_opposite_category_criterion(key: str, category: str) -> bool:
    """True if *key* belongs to the category opposite *category*."""
    if category == "tb":
        return key.startswith(_RTL_SIDE_PREFIXES)
    if category == "rtl":
        return key.startswith(_TB_SIDE_PREFIXES)
    return False


def _strip_detail_findings(detail: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *detail* containing only summary/count keys."""
    return {k: v for k, v in detail.items() if k in _DETAIL_SUMMARY_KEYS}


def project_state_for_category(
    state: dict[str, Any],
    category: str,
) -> dict[str, Any]:
    """Return a category-projected copy of a ``booley_state.json`` dict.

    Strips ``detail.pending`` / ``detail.resolved`` / ``detail.checks`` etc.
    from criteria whose key prefix names the opposite category. Counts and
    gating fields survive so threshold logic still works for an outside
    observer. Other state sections, including ``timeline``, pass through
    unchanged; the agent-capability-call ledger is not the leak channel.

    Idempotent and safe to call on non-dict input (returns *state* unchanged).
    """
    if not isinstance(state, dict) or category not in ("rtl", "tb"):
        return state
    out = dict(state)
    criteria = dict(out.get("criteria") or {})
    if not criteria:
        return out
    projected: dict[str, Any] = {}
    for key, entry in criteria.items():
        if (
            isinstance(entry, dict)
            and _is_opposite_category_criterion(key, category)
            and isinstance(entry.get("detail"), dict)
        ):
            new_entry = dict(entry)
            new_entry["detail"] = _strip_detail_findings(entry["detail"])
            projected[key] = new_entry
        else:
            projected[key] = entry
    out["criteria"] = projected
    return out


@contextmanager
def filter_state_file_for_category(
    state_path: Path | None,
    category: str,
) -> Generator[bool]:
    """Swap ``state_path`` for a category-projected copy for the duration.

    On enter: read the original bytes, write a projected copy in place.
    On exit (finally): restore the original bytes verbatim.

    Yields True if filtering was applied, False if skipped (no path, file
    missing, category neither rtl nor tb, or projection produced no change).

    Safety: the agent (LLM) is the only consumer of this file during the
    block; the Reviewer has no nested MCP tools that write to state, so the
    unconditional restore-on-exit cannot clobber concurrent writes. If that invariant ever breaks, switch to a merging
    restore that re-reads and diffs criteria before overwriting.
    """
    if state_path is None or category not in ("rtl", "tb"):
        yield False
        return
    try:
        if not state_path.is_file():
            yield False
            return
    except OSError:
        yield False
        return

    try:
        original_bytes = state_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "filter_state_file_for_category: could not read %s (%s) — passing through unfiltered",
            state_path,
            exc,
        )
        yield False
        return

    try:
        data = json.loads(original_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "filter_state_file_for_category: %s is not valid JSON (%s) — "
            "passing through unfiltered",
            state_path,
            exc,
        )
        yield False
        return

    projected = project_state_for_category(data, category)

    applied = False
    restored = False

    def _restore() -> None:
        # Idempotent: the signal handler restores first, then the finally
        # runs on the way out — the second call must not re-write the file.
        nonlocal restored
        if applied and not restored:
            restored = True
            _restore_state_bytes(state_path, original_bytes)

    try:
        # A projected state file left in place loses the opposite category's
        # findings; restore its original bytes even on fatal termination.
        with _restore_on_fatal_signal(_restore):
            if projected is not data:
                _write_projected_state(state_path, projected, category)
                applied = True
            yield applied
    finally:
        _restore()


def _write_projected_state(state_path: Path, projected: dict, category: str) -> None:
    """Atomically overwrite ``state_path`` with the category-projected copy.

    Writes to a ``.filter_tmp`` sibling then replaces, mirroring
    ``DevelopmentState.save`` semantics.
    """
    tmp_path = state_path.with_suffix(state_path.suffix + ".filter_tmp")
    tmp_path.write_text(
        json.dumps(projected, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(state_path)
    logger.info(
        "Isolation: projected %s for %s endpoint run (opposite-category detail stripped)",
        state_path.name,
        category,
    )


def _restore_state_bytes(state_path: Path, original_bytes: bytes) -> None:
    """Restore the original state bytes verbatim; log on failure (no raise)."""
    try:
        state_path.write_bytes(original_bytes)
    except OSError:
        logger.exception(
            "filter_state_file_for_category: could not restore %s — "
            "manual recovery may be required",
            state_path,
        )


@contextmanager
def _restore_on_fatal_signal(restore: Callable[[], None]) -> Generator[None]:
    """Run *restore* when a fatal signal would otherwise skip the ``finally``.

    SIGTERM/SIGHUP default to terminating the process outright — no stack
    unwind, no ``finally``, state left projected. Handling them restores
    the original state first, then re-raises with the default disposition
    so the caller's kill semantics are preserved. SIGINT chains to Python's
    own handler, which raises KeyboardInterrupt as usual.

    Only the main thread can install handlers; elsewhere this is a no-op
    (the ``finally`` path still covers ordinary exceptions).
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    signals = [
        s
        for s in (
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
            getattr(signal, "SIGINT", None),
        )
        if s is not None
    ]
    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        try:
            restore()
        finally:
            prev = previous.get(signum)
            if callable(prev):
                prev(signum, frame)  # e.g. default_int_handler -> KeyboardInterrupt
            else:
                signal.signal(signum, prev if prev is not None else signal.SIG_DFL)
                os.kill(os.getpid(), signum)

    installed: list[int] = []
    for sig in signals:
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (OSError, ValueError):  # pragma: no cover — platform-dependent
            continue
        installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            with suppress(OSError, ValueError):
                signal.signal(sig, previous[sig])
