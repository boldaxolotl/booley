"""Doctor chooses how inspection rebuilds the spec `booley init` would write."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from booley.harness import doctor, init_cmd
from booley.runtime import runtime_context


def test_expected_interactive_spec_is_init_rebuild_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the host the drift check rebuilds the spec through init's own builder."""
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)

    rebuild = doctor._expected_interactive_spec(tmp_path)

    assert isinstance(rebuild, functools.partial)
    assert rebuild.func is init_cmd.current_interactive_spec
    assert rebuild.args == (tmp_path,)


def test_expected_interactive_spec_is_none_inside_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the Sandbox host inputs are invisible, so no rebuild is offered."""
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)

    assert doctor._expected_interactive_spec(tmp_path) is None
