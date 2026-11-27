"""Every venue check, in this process and in a child, sees the location a test asks for."""

from __future__ import annotations

import sys
import textwrap

import pytest

from booley.config import agent
from booley.runtime import _codex_backend, runtime_context
from booley.runtime.runtime_context import RuntimeLocation
from tests.owned_scratch_support import child_environment
from tests.subprocess_budget import run_bounded_child

_CHILD = textwrap.dedent(
    """
    import sys
    from booley.config import agent
    from booley.runtime import _codex_backend, runtime_context
    from booley.runtime.runtime_context import RuntimeLocation
    from tests.sandbox_venue_fake import place_child

    place_child(RuntimeLocation[sys.argv[1]])
    print(
        runtime_context.runtime_location().name,
        runtime_context.container_marker_file(),
        agent._inside_container(),
        _codex_backend._inside_container(),
    )
    """
)


@pytest.mark.parametrize(
    ("location", "container_host", "marker"),
    [
        (RuntimeLocation.HOST, False, False),
        (RuntimeLocation.HOST, True, True),
        (RuntimeLocation.HAND_WRITTEN_SANDBOX, False, True),
        (RuntimeLocation.ISSUED_SANDBOX, False, True),
    ],
    ids=["host", "container-host", "hand-written-sandbox", "issued-sandbox"],
)
def test_every_venue_check_sees_the_placed_location(monkeypatch, location, container_host, marker):
    from tests.sandbox_venue_fake import place

    # A suite run inside a Sandbox inherits this; the fake must outrank it.
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")

    place(monkeypatch, location, container_host=container_host)

    assert runtime_context.runtime_location() is location
    assert runtime_context.container_marker_file() is marker
    assert agent._inside_container() is marker
    assert _codex_backend._inside_container() is marker


@pytest.mark.parametrize(
    ("location", "seen"),
    [("HOST", "HOST False False False"), ("ISSUED_SANDBOX", "ISSUED_SANDBOX True True True")],
    ids=["host", "issued-sandbox"],
)
def test_child_process_sees_the_placed_location(monkeypatch, tmp_path, location, seen):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    env = child_environment(tmp_path, tmp_path)

    # Outside the repository, so only PYTHONPATH lets the child import the fake.
    result = run_bounded_child([sys.executable, "-c", _CHILD, location], env=env, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == seen
