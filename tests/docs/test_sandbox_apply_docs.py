"""The printed Sandbox apply steps match SANDBOX.md's "Applying Sandbox changes" (#4182)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from booley.runtime.sandbox_apply import (
    APPLY_DOC_URL,
    UNKNOWN_SANDBOX_STEPS,
    Attachment,
    apply_steps,
    quote_arg,
)

SANDBOX_MD = Path(__file__).resolve().parents[2] / "docs" / "user" / "SANDBOX.md"
REBUILD = "Dev Containers: Rebuild Container"


def _section(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head : text.index(end, head + len(start))]


def _bullet(section: str, label: str) -> tuple[list[str], bool]:
    """Return the backticked commands of the ``**label:**`` bullet and its Rebuild step."""
    match = re.search(rf"^- \*\*{re.escape(label)}:\*\*(.*?)(?=^- |^\S|\Z)", section, re.M | re.S)
    assert match is not None, label
    body = " ".join(match.group(1).split())
    return re.findall(r"`([^`]+)`", body), REBUILD in body


def _documented() -> dict[tuple[Attachment, bool], tuple[list[str], bool]]:
    text = SANDBOX_MD.read_text(encoding="utf-8")
    managed = _section(text, "### After changing Sandbox settings", "### After upgrading")
    own = _section(text, "<summary>With your own image</summary>", "</details>")
    return {
        (Attachment.VSCODE, True): _bullet(managed, "In VS Code"),
        (Attachment.HEADLESS, True): _bullet(managed, "Headless"),
        (Attachment.VSCODE, False): _bullet(own, "In VS Code"),
        (Attachment.HEADLESS, False): _bullet(own, "Headless"),
    }


def _printed(steps: str) -> tuple[list[str], bool]:
    return re.findall(r"`([^`]+)`", steps), REBUILD in steps


@pytest.mark.parametrize("managed", [True, False], ids=["booley-image", "own-image"])
@pytest.mark.parametrize("attachment", list(Attachment))
def test_apply_steps_match_the_documented_workflow(attachment: Attachment, managed: bool) -> None:
    assert (
        _printed(apply_steps(attachment, managed=managed)) == _documented()[(attachment, managed)]
    )


@pytest.mark.parametrize("managed", [True, False], ids=["booley-image", "own-image"])
def test_unknown_owner_lists_the_vscode_then_the_headless_workflow(managed: bool) -> None:
    documented = _documented()
    vscode, rebuild = documented[(Attachment.VSCODE, managed)]
    headless, headless_rebuild = documented[(Attachment.HEADLESS, managed)]

    commands, has_rebuild = _printed(apply_steps(None, managed=managed))

    assert commands == [*vscode, *headless]
    assert has_rebuild == (rebuild or headless_rebuild)


def test_project_scoped_steps_keep_the_documented_commands() -> None:
    commands, _ = _printed(apply_steps(Attachment.HEADLESS, managed=False, project="/work/p"))

    assert [c.split(" --project")[0] for c in commands] == _documented()[
        (Attachment.HEADLESS, False)
    ][0]
    assert all(c.endswith(f"--project {quote_arg('/work/p')}") for c in commands)


def test_unknown_sandbox_steps_point_at_the_documented_own_image_section() -> None:
    assert APPLY_DOC_URL in UNKNOWN_SANDBOX_STEPS
    assert "## Applying Sandbox changes" in SANDBOX_MD.read_text(encoding="utf-8")
