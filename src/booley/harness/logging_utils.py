"""Logging utilities -- incident logging, transition helpers, file handler setup."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from booley.timefmt import format_human_datetime, utc_now_rfc3339

from .colors import bold_red, yellow

logger = logging.getLogger(__name__)


class TerseFormatter(logging.Formatter):
    """Console formatter: step-prefixed, terse for INFO, flagged for WARNING+.

    INFO/DEBUG  -> "HH:MM:SS [planning] message"
    WARNING     -> "HH:MM:SS WARNING [planning] message"   (red)
    ERROR/CRIT  -> "HH:MM:SS ERROR [planning] message"     (bold red)
    No step    -> "HH:MM:SS message"  (before ticket intake completes)

    When no step tag is needed (e.g. loop runner), the formatter omits it.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, self.datefmt)
        step = get_current_step()
        tag = f"[{step}] " if step else ""
        if record.levelno >= logging.ERROR:
            return bold_red(f"{ts} {record.levelname} {tag}{record.getMessage()}")
        if record.levelno >= logging.WARNING:
            return yellow(f"{ts} {record.levelname} {tag}{record.getMessage()}")
        return f"{ts} {tag}{record.getMessage()}"


class HumanDateFormatter(logging.Formatter):
    """Logging formatter whose full timestamp follows Booley's human format."""

    def formatTime(  # noqa: N802 — stdlib logging.Formatter defines this camelCase hook
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        del datefmt
        return format_human_datetime(datetime.fromtimestamp(record.created), seconds=True)


# Module-level logging state, guarded by _lock so concurrent access
# (e.g. from a heartbeat thread) is safe.
_lock = threading.Lock()
_file_handler: logging.FileHandler | None = None
_current_step: str = ""


def set_current_step(step: str) -> None:
    """Update the current step name (shown in console log prefix)."""
    global _current_step
    with _lock:
        _current_step = step


def get_current_step() -> str:
    """Get the current step name for log formatting."""
    with _lock:
        return _current_step


def setup_file_logging(log_path_or_dir: Path) -> None:
    """Attach a FileHandler to the root logger.

    Call once after the ticket slug is resolved so all subsequent log output
    (from every module) is persisted alongside other ticket artifacts.
    """
    global _file_handler
    with _lock:
        if _file_handler is not None:
            return  # already attached

        log_path = (
            log_path_or_dir / "harness.log" if log_path_or_dir.suffix == "" else log_path_or_dir
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)

        _file_handler = logging.FileHandler(log_path, encoding="utf-8")
        _file_handler.setLevel(logging.DEBUG)  # capture everything to file
        _file_handler.setFormatter(
            HumanDateFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(_file_handler)
    logger.debug("File logging started -> %s", log_path)


def teardown_file_logging() -> None:
    """Remove the file handler (called on harness exit)."""
    global _file_handler
    with _lock:
        if _file_handler is not None:
            logging.getLogger().removeHandler(_file_handler)
            _file_handler.close()
            _file_handler = None


def now_iso() -> str:
    """UTC ISO-8601 timestamp for embedding in markdown artifacts."""
    return utc_now_rfc3339()
