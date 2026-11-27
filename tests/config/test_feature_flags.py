"""Maintainer-only Feature Flags: registry, query, and configuration loading."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.conftest import FeatureFlagHelper
from tests.harness.doctor_run_support import write_project

from booley.harness import booley as cli
from booley.harness.setup import readiness


def _project(root: Path) -> Path:
    booley_dir = root / ".booley"
    booley_dir.mkdir(parents=True)
    (booley_dir / "booley.toml").write_bytes(b'[project]\nname = "unit"\n')
    return booley_dir


@pytest.mark.parametrize("case", ["outside-a-project", "no-features-table", "listed"])
def test_registered_flag_follows_the_project_enable_list(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature_flags: FeatureFlagHelper
) -> None:
    from booley.config import feature_flags as flags

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    feature_flags.register("test-flag")
    if case == "outside-a-project":
        root = tmp_path
    else:
        root = tmp_path / "proj"
        booley_dir = _project(root)
        if case == "listed":
            feature_flags.enable(booley_dir, "test-flag")
    monkeypatch.chdir(root)

    assert flags.feature_enabled("test-flag") is (case == "listed")


def test_querying_an_unregistered_flag_raises() -> None:
    from booley.config import feature_flags as flags

    with pytest.raises(flags.UnknownFeatureFlagError, match="ghost"):
        flags.feature_enabled("ghost")


def test_unregistered_enable_name_fails_configuration_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    feature_flags: FeatureFlagHelper,
) -> None:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    booley_dir = write_project(root)
    feature_flags.enable(booley_dir, "test-flag")
    toml = booley_dir / "booley.toml"
    toml.write_bytes(toml.read_bytes().replace(b'["test-flag"]', b'["ghost"]'))
    outside = tmp_path / "outside"
    outside.mkdir()

    # Dispatch checks the selected Project, wherever the command runs from.
    monkeypatch.chdir(outside)
    monkeypatch.setattr("sys.argv", ["booley", "-C", str(root), "targets"])
    assert cli.main() == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR:")
    assert "ghost" in err
    assert "test-flag" in err

    # Parsing never fails, so --version still works from inside that Project.
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.argv", ["booley", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code in (0, None)

    failures = [
        f.message for f in readiness.load_project(root).report.findings if f.severity == "fail"
    ]
    assert any("ghost" in message and "test-flag" in message for message in failures)


def test_registered_flags_are_well_formed() -> None:
    from booley.config import feature_flags as flags

    names = [flag.name for flag in flags.FEATURE_FLAGS]
    assert len(names) == len(set(names))
    for flag in flags.FEATURE_FLAGS:
        assert re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", flag.name), flag.name
        assert flag.summary.strip() and "\n" not in flag.summary, flag.name
        assert type(flag.issue) is int
        assert flag.issue > 0, flag.name
