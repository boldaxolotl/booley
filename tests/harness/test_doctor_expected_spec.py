"""Doctor chooses how inspection rebuilds the spec `booley init` would write."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from booley.harness import doctor, init_cmd
from booley.runtime import runtime_context


@pytest.mark.parametrize("inside", [False, True], ids=["host", "sandbox"])
def test_expected_sandbox_spec_depends_on_the_venue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: bool
) -> None:
    """On the host the drift check rebuilds through init's own builder; inside, it cannot."""
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: inside)

    # private: every whole run replaces `_expected_sandbox_spec` (`_patch_environment`), since
    # the real host rebuild needs a Sandbox Image a test host lacks; no other test checks which
    # spec inspection compares against.
    rebuild = doctor._expected_sandbox_spec(tmp_path)

    if inside:
        assert rebuild is None
        return
    assert isinstance(rebuild, functools.partial)
    assert rebuild.func is init_cmd.current_interactive_spec
    assert rebuild.args == (tmp_path,)
