"""BWave's schema URL stays pinned to the current release tag on ``main``.

The release checker requires one canonical URL,
``https://raw.githubusercontent.com/boldaxolotl/booley/v<VERSION>/crates/bwave/schema/bwave.json``,
in the emitted envelope, the schema ``$id``, and the reference docs. Between
releases ``VERSION`` names the last release, so that URL must already be in
place; a drifted pin (a commit SHA, a URN ``$id``) otherwise surfaces only at
release time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Any raw-GitHub URL ending in bwave.json, so a commit-pinned URL is caught too.
SCHEMA_URL = re.compile(r"https://raw\.githubusercontent\.com/[^\s\"'`]+/bwave\.json")
SCHEMA_FILES = (
    "crates/bwave/src/output.rs",
    "crates/bwave/schema/bwave.json",
    "crates/bwave/docs/public/reference/json-envelope.md",
)


def _canonical_url() -> str:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    return (
        f"https://raw.githubusercontent.com/boldaxolotl/booley/v{version}"
        "/crates/bwave/schema/bwave.json"
    )


@pytest.mark.parametrize("relative", SCHEMA_FILES)
def test_schema_urls_use_the_current_release_tag(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")

    assert set(SCHEMA_URL.findall(text)) == {_canonical_url()}


def test_schema_id_is_the_canonical_url() -> None:
    schema = json.loads((ROOT / "crates/bwave/schema/bwave.json").read_text(encoding="utf-8"))

    assert schema["$id"] == _canonical_url()
