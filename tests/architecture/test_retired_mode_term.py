"""Guard the retired mode name out of source and tests (issue 4546)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "tests")
_THIS_FILE = Path(__file__).resolve()
_PHRASE = re.compile(r"interactive[\s#-]+mode", re.IGNORECASE)
_ALLOWED_PHRASE_FILES = (
    _ROOT / "src" / "booley" / "data" / "refs" / "CHANGELOG.md",
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
)


def _files(suffixes: tuple[str, ...] | None = None) -> list[Path]:
    found: list[Path] = []
    for name in _SCAN_DIRS:
        for path in sorted((_ROOT / name).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if suffixes is not None and path.suffix not in suffixes:
                continue
            found.append(path)
    return found


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
