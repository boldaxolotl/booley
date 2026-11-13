"""Windows pipe readiness helper: refuses to run on other platforms."""

from __future__ import annotations

import pytest

from booley.runtime import pipe


def test_windows_pipe_readable_refuses_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipe.sys, "platform", "linux")

    with pytest.raises(OSError, match="Windows-only"):
        pipe.windows_pipe_readable(0)
