"""Nested Codex HOME environment and destination preservation."""

import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
from tests.conftest import require_symlinks
from tests.owned_scratch_support import complete_runs, make_repo


def _isolate_scratch(tmp_path, monkeypatch) -> Path:
    """Point the system temp directory (and the login home) inside *tmp_path*."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return scratch


def _real_login(tmp_path) -> Path:
    login = tmp_path / "home" / ".codex" / "auth.json"
    login.parent.mkdir(parents=True)
    login.write_text('{"tokens":{"access_token":"SYNTHETIC_OLD"}}', encoding="utf-8")
    return login


def test_nested_codex_auth_links_real_login(tmp_path, monkeypatch):
    """AC1: the nested home holds a link to the real login, never a copy."""
    from booley.runtime import _codex_backend as cb

    require_symlinks(tmp_path)
    _isolate_scratch(tmp_path, monkeypatch)
    login = _real_login(tmp_path)
    monkeypatch.setattr(cb, "_NESTED_HOMES", {})
    nested = Path(cb._ensure_nested_codex_home("link", ["lint"]))
    try:
        auth = nested / ".codex" / "auth.json"
        assert auth.is_symlink()
        assert auth.resolve() == login.resolve()
        assert [p for p in nested.rglob("auth.json") if not p.is_symlink()] == []
    finally:
        shutil.rmtree(nested, ignore_errors=True)


def test_refresh_through_nested_home_reaches_real_login(tmp_path, monkeypatch):
    """AC2: a token refresh written through the nested path lands in the real login."""
    from booley.runtime import _codex_backend as cb

    require_symlinks(tmp_path)
    _isolate_scratch(tmp_path, monkeypatch)
    login = _real_login(tmp_path)
    monkeypatch.setattr(cb, "_NESTED_HOMES", {})
    nested = Path(cb._ensure_nested_codex_home("refresh", ["lint"]))
    try:
        refreshed = '{"tokens":{"access_token":"SYNTHETIC_REFRESHED"}}'
        (nested / ".codex" / "auth.json").write_text(refreshed, encoding="utf-8")
        assert login.read_text(encoding="utf-8") == refreshed
    finally:
        shutil.rmtree(nested, ignore_errors=True)


@pytest.mark.parametrize("policy", ["auto", "subscription", "api_key", "gateway"])
def test_missing_login_leaves_no_auth_entry(tmp_path, monkeypatch, policy):
    """No login, or a policy that excludes it: no file and no dangling link."""
    from booley.runtime import _codex_backend as cb

    require_symlinks(tmp_path)
    _isolate_scratch(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", "https://gateway.example.test/v1")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC")
    monkeypatch.setattr(cb, "_NESTED_HOMES", {})

    def ensure() -> Path:
        cb._NESTED_HOMES.clear()
        return Path(cb._ensure_nested_codex_home("missing", ["lint"], auth_mode=policy))

    auth = ensure() / ".codex" / "auth.json"
    assert not os.path.lexists(auth)
    login = _real_login(tmp_path)
    ensure()
    # Only the policies that may use the login link it.
    assert auth.is_symlink() is (policy in {"auto", "subscription"})
    login.unlink()
    ensure()
    assert not os.path.lexists(auth)


@pytest.mark.skipif(sys.platform == "win32", reason="nested homes exist only in the Linux Sandbox")
def test_completed_runs_leave_no_nested_home(tmp_path):
    """AC3: after several completed runs in one process, no nested home remains."""
    require_symlinks(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _real_login(tmp_path)
    repo = make_repo(tmp_path / "repo")
    homes = complete_runs(repo, scratch, tmp_path / "home")
    try:
        assert len(homes) == 3
        assert [home for home in homes if home.exists()] == []
    finally:
        for home in homes:  # unfixed code leaves these in the real /tmp
            shutil.rmtree(home, ignore_errors=True)


def test_nested_codex_home_includes_scoped_environment(tmp_path, monkeypatch):
    from booley.runtime import _codex_backend as cb

    _isolate_scratch(tmp_path, monkeypatch)
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
    _isolate_scratch(tmp_path, monkeypatch)
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
    _isolate_scratch(tmp_path, monkeypatch)
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
