"""Goal Mode is removed: ``booley goal`` no longer parses."""

from __future__ import annotations

import pytest

from booley.harness import booley as cli


def test_goal_is_an_unknown_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["booley", "goal", "status"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'goal'" in err
