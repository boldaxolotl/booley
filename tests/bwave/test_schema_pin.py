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
SCHEMA_URL_PATTERN = re.compile(r"https://raw\.githubusercontent\.com/[^\s\"'`]+/bwave\.json")
# Release tags named in prose: the release checker's patterns, plus any
# tag-pinned schema path, which also covers the abbreviated ``.../vX.Y.Z/...`` form.
_TAG = r"(?P<tag>v[0-9]+\.[0-9]+\.[0-9]+)"
RELEASE_PIN_PATTERNS = (
    re.compile(rf"currently\s+`{_TAG}`", re.IGNORECASE),
    re.compile(rf"pinned to the\s+{_TAG}\s+release tag", re.IGNORECASE),
    re.compile(rf"emits\s+`[^`]*{_TAG}[^`]*`", re.IGNORECASE),
    re.compile(rf"/{_TAG}/crates/bwave/schema/bwave\.json"),
)
SCHEMA_FILES = (
    "crates/bwave/src/output.rs",
    "crates/bwave/schema/bwave.json",
    "crates/bwave/docs/public/reference/json-envelope.md",
)


def _release_tag() -> str:
    return "v" + (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _canonical_url() -> str:
    return (
        f"https://raw.githubusercontent.com/boldaxolotl/booley/{_release_tag()}"
        "/crates/bwave/schema/bwave.json"
    )


@pytest.mark.parametrize("relative", SCHEMA_FILES)
def test_schema_urls_use_the_current_release_tag(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")

    assert set(SCHEMA_URL_PATTERN.findall(text)) == {_canonical_url()}


@pytest.mark.parametrize("relative", SCHEMA_FILES)
def test_prose_pins_name_the_current_release_tag(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    pins = {match["tag"] for pattern in RELEASE_PIN_PATTERNS for match in pattern.finditer(text)}

    assert pins <= {_release_tag()}


def test_schema_id_is_the_canonical_url() -> None:
    schema = json.loads((ROOT / "crates/bwave/schema/bwave.json").read_text(encoding="utf-8"))

    assert schema["$id"] == _canonical_url()
