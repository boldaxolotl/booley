"""The ``booley projects forget`` leftovers listing is an aligned table."""

from __future__ import annotations

import importlib
import importlib.util

import pytest

from booley.harness import colors
from booley.harness.terminal_table import render_table
from booley.projects.image_keepers import Leftover


def test_leftovers_render_as_a_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    assert importlib.util.find_spec("booley.harness.projects_view"), "leftovers view missing"
    view = importlib.import_module("booley.harness.projects_view")
    leftovers = (
        Leftover("image", "sha256:abc", "shared", size="1GB", tags=("one:1", "two:2")),
        Leftover("volume", "state-vol", "unused"),
    )

    assert view.render_leftovers(leftovers) == "\n".join(
        render_table(
            ["Kind", "Name", "Size", "Reason", "Tags"],
            [
                ["image", "sha256:abc", "1GB", "shared", "one:1, two:2"],
                ["volume", "state-vol", "", "unused", ""],
            ],
        )
    )
