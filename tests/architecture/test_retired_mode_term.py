"""Guard the retired mode name out of source and tests (issues 4546 and 4547)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "tests")
_THIS_FILE = Path(__file__).resolve()
_PHRASE = re.compile(r"interactive[\s#-]+mode", re.IGNORECASE)
_ALLOWED_PHRASE_FILES = (
    _ROOT / "src" / "booley" / "data" / "skills" / "booley-setup" / "steps" / "0-plan.md",
)
_OLD_PRIVATE_NAMES = (
    "_step_interactive",
    "_step_interactive_files",
    "_select_interactive_app",
    "_InteractiveSpecSources",
    "_interactive_spec_sources",
    "_current_interactive_spec_sources",
    "_report_interactive_state",
    "_report_interactive_changes",
    "_interactive_precondition_failed",
    "_inspect_interactive_plan",
    "_apply_interactive_plan",
    "_expected_interactive_spec",
    "_running_interactive_containers",
    "_strict_all_interactive_states",
    "_strict_running_interactive_states",
    "_strict_interactive_states",
    "_check_interactive_state_volumes",
    "_noninteractive_relay",
    "_validate_interactive_args",
    "_DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS",
    "_DEFAULT_INTERACTIVE_MAX_AGE_SECONDS",
    "_seed_interactive",
    "_interactive_project",
    "_auth_interactive_ports",
    "interactive_docker",
    "_interactive_logs_dir",
    "_maybe_configure_interactive_logs_dir",
    "INTERACTIVE_LOGS_DIR_NAME",
    "_check_interactive_logs_tracked",
)
_RETIRED_VARIABLE_OR_DIRECTORY = re.compile(r"BOOLEY_MCP_MODE|\.interactive_logs")
_HISTORY_PREFIXES = (
    "CHANGELOG.md",
    "changelog.d/",
    "docs/adr/",
)
# The only lines allowed to name the retired variable or directory, counted per file: the
# Doctor leftover check and its guarded behavior, and the Sandbox issued before the rename.
_ALLOWED_HITS = {
    "src/booley/harness/doctor.py": 1,
    "tests/harness/test_doctor.py": 1,
    "tests/runtime/test_session_issuance.py": 1,
}


def _files(suffixes: tuple[str, ...] | None = None) -> list[Path]:
    # Git-listed files only: a local build renders Git-ignored copies of the changelog.
    return [
        path
        for path in source_checkout_files(_ROOT, *_SCAN_DIRS)
        if "__pycache__" not in path.parts and (suffixes is None or path.suffix in suffixes)
    ]


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []


def test_no_source_or_test_text_names_the_retired_mode() -> None:
    hits: list[str] = []
    for path in _files():
        if path.resolve() in _ALLOWED_PHRASE_FILES:
            continue
        text = "\n".join(_lines(path))
        for match in _PHRASE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(_ROOT).as_posix()}:{line}")

    assert hits == []


def test_docker_module_and_private_names_are_renamed() -> None:
    assert importlib.util.find_spec("booley.runtime.interactive_docker") is None
    assert importlib.util.find_spec("booley.runtime.host_docker") is not None

    pattern = re.compile(r"\b(?:" + "|".join(_OLD_PRIVATE_NAMES) + r")\b")
    hits: list[str] = []
    for path in _files((".py",)):
        if path.resolve() == _THIS_FILE:
            continue
        for number, line in enumerate(_lines(path), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(_ROOT).as_posix()}:{number}")

    assert hits == []


def test_status_tool_description_uses_no_retired_mode_name() -> None:
    from booley.mcp import catalog

    definition = catalog._status_mcp_tool_def()  # pyright: ignore[reportPrivateUsage]

    assert not _PHRASE.search(definition["description"])


def test_no_tracked_text_names_the_retired_variable_or_log_directory() -> None:
    counts: dict[str, int] = {}
    for path in source_checkout_files(_ROOT):
        name = path.relative_to(_ROOT).as_posix()
        if path.resolve() == _THIS_FILE or name.startswith(_HISTORY_PREFIXES):
            continue
        hits = sum(1 for line in _lines(path) if _RETIRED_VARIABLE_OR_DIRECTORY.search(line))
        if hits:
            counts[name] = hits

    assert counts == _ALLOWED_HITS
