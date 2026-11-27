"""The printed ``booley session`` steps match SANDBOX.md's "Applying Sandbox changes" (#4182)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from booley.core.host_shell import quote_arg
from booley.runtime.sandbox_apply import (
    APPLY_DOC_URL,
    UNKNOWN_SANDBOX_STEPS,
    Attachment,
    apply_steps,
)

SANDBOX_MD = Path(__file__).resolve().parents[2] / "docs" / "user" / "SANDBOX.md"


def _section(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head : text.index(end, head + len(start))]


def _commands(section: str) -> list[str]:
    """Return the backticked commands of the section's "Run ... on the host" step."""
    match = re.search(r"\bRun\s+(`booley.*?)\s+on\s+the\s+host", section, re.S)
    assert match is not None, section
    return re.findall(r"`([^`]+)`", " ".join(match.group(1).split()))


def _documented() -> dict[bool, list[str]]:
    """The documented commands, keyed by whether Booley manages the image."""
    text = SANDBOX_MD.read_text(encoding="utf-8")
    managed = _section(text, "### After changing Sandbox settings", "### After upgrading")
    own = _section(text, "<summary>With your own image</summary>", "</details>")
    return {True: _commands(managed), False: _commands(own)}


def _printed(steps: str) -> list[str]:
    return re.findall(r"`([^`]+)`", steps)


@pytest.mark.parametrize("managed", [True, False], ids=["booley-image", "own-image"])
def test_apply_steps_match_the_documented_workflow(managed: bool) -> None:
    assert _printed(apply_steps(Attachment.HEADLESS, managed=managed)) == _documented()[managed]


def test_project_scoped_steps_keep_the_documented_commands() -> None:
    commands = _printed(apply_steps(Attachment.HEADLESS, managed=False, project="/work/p"))

    assert [c.split(" --project")[0] for c in commands] == _documented()[False]
    assert all(c.endswith(f"--project {quote_arg('/work/p')}") for c in commands)


def test_unknown_sandbox_steps_point_at_the_documented_own_image_section() -> None:
    assert APPLY_DOC_URL in UNKNOWN_SANDBOX_STEPS
    assert "## Applying Sandbox changes" in SANDBOX_MD.read_text(encoding="utf-8")


def test_unknown_sandbox_steps_match_the_documented_commands() -> None:
    documented = _documented()[True]

    assert _printed(UNKNOWN_SANDBOX_STEPS)[-len(documented) :] == documented
