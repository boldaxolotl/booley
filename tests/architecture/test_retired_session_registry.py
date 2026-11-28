"""Guard the retired Session Registry and session-activity code out (issue 4569)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_THIS_FILE = Path(__file__).resolve()
_RETIRED = re.compile(
    r"Session Registry|SessionRegistry|session_registry|[Ss]ession[ _-]activity"
    r"|[Ss]ession handles?\b|S<[nN]>|S\?|reserve_session_handle"
)
_HISTORY_PREFIXES = (
    "CHANGELOG.md",
    "src/booley/data/refs/CHANGELOG.md",
    "changelog.d/",
    "docs/adr/",
)
# The only lines allowed to name them, counted per file: the checks that the image no
# longer carries the hook.
_ALLOWED_HITS = {".github/contracts/session-runtime.toml": 1, "tests/ci/test_image_contract.py": 1}


@pytest.mark.parametrize(
    "module",
    [
        "booley.mcp.session_registry",
        "booley.mcp.session_observer",
        "booley.runtime.session_activity",
        "booley.harness.activity_hooks",
        "booley.harness.dashboard.sessions",
    ],
)
def test_retired_session_modules_are_gone(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def _hit_lines(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return 0
    return sum(1 for line in lines if _RETIRED.search(line))


def test_no_tracked_text_names_the_session_registry_or_session_handles() -> None:
    counts: dict[str, int] = {}
    for path in source_checkout_files(_ROOT):
        name = path.relative_to(_ROOT).as_posix()
        if path.resolve() == _THIS_FILE or name.startswith(_HISTORY_PREFIXES):
            continue
        if hits := _hit_lines(path):
            counts[name] = hits

    assert counts == _ALLOWED_HITS
