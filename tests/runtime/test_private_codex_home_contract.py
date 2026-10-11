"""Nested Codex HOME environment and destination preservation."""

import tomllib
from pathlib import Path

import pytest


def test_nested_codex_home_includes_scoped_environment(tmp_path, monkeypatch):
    from booley.runtime import _codex_backend as cb

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BOOLEY_SLUG", "coverage")
    cb._NESTED_HOMES.clear()

    home = Path(
        cb._ensure_nested_codex_home(
            "coverage_analyst",
            ["coverage_evidence"],
            {"BOOLEY_COVERAGE_CAMPAIGN": "/reports/coverage.json"},
        )
    )

    config = (home / ".codex/config.toml").read_text(encoding="utf-8")
    assert 'BOOLEY_COVERAGE_CAMPAIGN = "/reports/coverage.json"' in config
    assert 'BOOLEY_NESTED_MCP_TOOLS = "coverage_evidence"' in config


@pytest.mark.parametrize("preference", [None, True, False])
@pytest.mark.parametrize("toml11", [False, True])
def test_private_codex_writers_preserve_destination_choice(
    tmp_path, monkeypatch, preference, toml11, caplog
):
    from booley.runtime import _codex_backend as cb

    real_path = Path

    def isolated_path(value):
        value = str(value)
        return tmp_path / Path(value).name if value.startswith("/tmp/codex-") else real_path(value)

    monkeypatch.setattr(cb, "Path", isolated_path)
    monkeypatch.setenv("HOME", str(tmp_path / "ambient"))
    monkeypatch.setattr(cb, "_NESTED_HOMES", {})
    create = cb._ensure_nested_codex_home
    home = real_path(create("test", ["lint"]))
    config = home / ".codex/config.toml"
    assert tomllib.loads(config.read_text())["suppress_unstable_features_warning"] is True
    if preference is not None or toml11:
        choice = (
            ""
            if preference is None
            else f"suppress_unstable_features_warning={str(preference).lower()}\n"
        )
        config.write_text(choice + ("features={\n mcp_2026_07_28=true,\n}\n" if toml11 else ""))
    before = config.read_bytes()
    assert real_path(create("test", ["lint"])) == home
    assert config.read_bytes() == before
    cb._NESTED_HOMES.clear()
    assert real_path(create("test", ["lint"])) == home
    parsed = tomllib.loads(config.read_text())
    assert "malformed" not in caplog.text
    assert parsed["suppress_unstable_features_warning"] is (
        True if preference is None else preference
    )
    assert parsed["features"]["mcp_2026_07_28"] is True
    env = parsed["mcp_servers"]["booley"]["env"]
    assert env["BOOLEY_NESTED_MCP_TOOLS"] == "lint"


@pytest.mark.parametrize("existing", ['broken="', 'suppress_unstable_features_warning="bad"'])
def test_private_codex_writer_invalid_destination(tmp_path, monkeypatch, existing, caplog):
    from booley.runtime import _codex_backend as cb

    real_path = Path
    monkeypatch.setattr(
        cb,
        "Path",
        lambda value: (
            tmp_path / real_path(value).name
            if str(value).startswith("/tmp/codex-")
            else real_path(value)
        ),
    )
    monkeypatch.setenv("HOME", str(tmp_path / "ambient"))
    monkeypatch.setattr(cb, "_NESTED_HOMES", {})
    create = cb._ensure_nested_codex_home
    config = real_path(create("test", ["lint"])) / ".codex/config.toml"
    config.write_text(existing)
    cb._NESTED_HOMES.clear()
    if existing.startswith("broken"):
        create("test", ["lint"])
        assert tomllib.loads(config.read_text())["suppress_unstable_features_warning"] is True
        assert "malformed" in caplog.text
    else:
        with pytest.raises(ValueError, match="boolean"):
            create("test", ["lint"])
        assert config.read_text() == existing
