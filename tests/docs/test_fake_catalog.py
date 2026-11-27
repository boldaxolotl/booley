"""The test fakes catalog links every fake and double module under ``tests/``."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "docs/internals/TEST_FAKES.md"


def test_catalog_rows_match_the_fake_modules() -> None:
    lines = CATALOG.read_text(encoding="utf-8").splitlines()
    rows = [line for line in lines if line.startswith("|")]
    named = {
        path for row in rows for path in re.findall(r"\]\(\.\./\.\./(tests/[^)#\s]+\.py)\)", row)
    }
    modules = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").rglob("*.py")
        if re.search("fake|double", path.stem) and not path.stem.startswith("test_")
    }
    assert sorted(modules - named) == [], "fake or double modules with no catalog row"
    missing = sorted(path for path in named if not (REPO_ROOT / path).is_file())
    assert missing == [], "catalog rows naming a module that does not exist"
