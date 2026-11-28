"""Nested Codex HOME: login link, scoped environment, destination handling and races."""

import asyncio
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
from tests.codex_cli_fake import codex_report, install_fake_codex, write_codex_login
from tests.conftest import require_symlinks
from tests.owned_scratch_support import (
    complete_runs,
    gate_marker_publishes,
    make_repo,
    run_together,
)
from tests.time_budget import scaled

from booley.runtime import _codex_backend as cb
from booley.runtime import auth_token


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """A Sandbox-like process: nested homes under *tmp_path*, a fake ``codex`` on ``PATH``."""
    install_fake_codex(tmp_path, monkeypatch)


def _config(report: dict) -> Path:
    return Path(report["home"]) / "config.toml"


@pytest.mark.asyncio
async def test_nested_codex_home_links_the_real_login_and_carries_scoped_environment(
    fake_codex, tmp_path
):
    """The nested home holds a link to the real login, never a copy, and the scoped MCP env."""
    require_symlinks(tmp_path)
    login = write_codex_login(tmp_path)
    scoped = {
        "label": "coverage_analyst",
        "nested_mcp_tools": ["coverage_evidence"],
        "nested_mcp_env": {"BOOLEY_COVERAGE_RECORD": "/reports/coverage.json"},
    }

    report = await codex_report(tmp_path, **scoped)

    nested = Path(report["home"]).parent
    try:
        auth = nested / ".codex" / "auth.json"
        assert auth.is_symlink()
        assert auth.resolve() == login.resolve()
        assert [p for p in nested.rglob("auth.json") if not p.is_symlink()] == []
        # A token refresh written through the nested path lands in the real login.
        refreshed = '{"tokens":{"access_token":"SYNTHETIC_REFRESHED"}}'
        auth.write_text(refreshed, encoding="utf-8")
        assert login.read_text(encoding="utf-8") == refreshed
        config = _config(report).read_text(encoding="utf-8")
        assert 'BOOLEY_COVERAGE_RECORD = "/reports/coverage.json"' in config
        assert 'BOOLEY_NESTED_MCP_TOOLS = "coverage_evidence"' in config
        # A cache hit leaves the destination untouched.
        with _config(report).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("# user edit\n")
        before = _config(report).read_bytes()
        again = await codex_report(tmp_path, **scoped)
        assert again["home"] == report["home"]
        assert _config(report).read_bytes() == before
    finally:
        shutil.rmtree(nested, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="nested homes exist only in the Linux Sandbox")
@pytest.mark.parametrize("auth_mode", ["api_key", "gateway"])
async def test_nested_codex_home_leaves_the_login_out_for_a_non_subscription_mode(
    fake_codex, tmp_path, monkeypatch, auth_mode
):
    """A mode that must not use the stored login gets a nested home without it (#1537)."""
    require_symlinks(tmp_path)
    write_codex_login(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_OPENAI")
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_TOKEN")
    report = await codex_report(tmp_path, auth_mode=auth_mode, nested_mcp_tools=["lint"])
    try:
        assert report["auth_json_exists"] is False
    finally:
        shutil.rmtree(Path(report["home"]).parent, ignore_errors=True)


@pytest.mark.parametrize("policy", [auth_token.VISITOR_AUTH, "subscription", "api_key", "gateway"])
def test_missing_login_leaves_no_auth_entry(tmp_path, policy):
    """No login, or a policy that excludes it: no file and no dangling link."""
    require_symlinks(tmp_path)
    login = tmp_path / "home" / ".codex" / "auth.json"
    auth = tmp_path / "private" / "auth.json"
    auth.parent.mkdir()

    auth_token.link_codex_auth(login, auth, policy)
    assert not os.path.lexists(auth)
    write_codex_login(tmp_path)
    auth_token.link_codex_auth(login, auth, policy)
    # Only the policies that may use the login link it.
    assert auth.is_symlink() is (policy in {auth_token.VISITOR_AUTH, "subscription"})
    login.unlink()
    auth_token.link_codex_auth(login, auth, policy)
    assert not os.path.lexists(auth)


@pytest.mark.skipif(sys.platform == "win32", reason="nested homes exist only in the Linux Sandbox")
def test_completed_runs_leave_no_nested_home(tmp_path):
    """After several completed runs in one process, no nested home remains."""
    require_symlinks(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    write_codex_login(tmp_path)
    repo = make_repo(tmp_path / "repo")
    homes = complete_runs(repo, scratch, tmp_path / "home")
    try:
        assert len(homes) == 3
        assert [home for home in homes if home.exists()] == []
    finally:
        for home in homes:  # unfixed code leaves these in the real /tmp
            shutil.rmtree(home, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing",
    [
        'broken="',
        'suppress_unstable_features_warning="bad"',
        "suppress_unstable_features_warning=false\n",
    ],
)
async def test_private_codex_writer_invalid_destination(fake_codex, tmp_path, existing, caplog):
    write_codex_login(tmp_path)
    config = _config(await codex_report(tmp_path, nested_mcp_tools=["lint"]))
    config.write_text(existing, encoding="utf-8")
    # Private on purpose: only a new process regenerates a home, and no public entry point
    # empties this process's cache. The fake parses the config, so no call may run on the
    # broken text without this.
    cb._NESTED_HOMES.clear()

    if existing.startswith("broken"):
        await codex_report(tmp_path, nested_mcp_tools=["lint"])
        assert (
            tomllib.loads(config.read_text(encoding="utf-8"))["suppress_unstable_features_warning"]
            is True
        )
        assert "malformed" in caplog.text
    elif "false" in existing:
        # The destination's own preference survives regeneration.
        await codex_report(tmp_path, nested_mcp_tools=["lint"])
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        assert parsed["suppress_unstable_features_warning"] is False
        assert parsed["mcp_servers"]["booley"]["env"]["BOOLEY_NESTED_MCP_TOOLS"] == "lint"
    else:
        with pytest.raises(ValueError, match="boolean"):
            await codex_report(tmp_path, nested_mcp_tools=["lint"])
        assert config.read_text(encoding="utf-8") == existing


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="nested homes exist only in the Linux Sandbox"
)
def test_parallel_callers_share_one_home_per_cache_key(fake_codex, tmp_path, monkeypatch):
    """Parallel callers get one home per key, created exactly once."""
    from booley.runtime.pid import capture_process_identity

    write_codex_login(tmp_path)
    # "Created exactly once" is not visible in a home's contents and no public
    # entry point reaches the cache, so record each creation's marking.
    marked: list[Path] = []
    real_mark = cb.mark_owned

    def spy(directory: Path) -> None:
        marked.append(directory)
        real_mark(directory)

    monkeypatch.setattr(cb, "mark_owned", spy)
    gate_marker_publishes(monkeypatch, 2, scaled(1.0))
    calls = iter(range(8))

    def call(tool: str) -> Path:
        transcript = tmp_path / f"transcript-{next(calls)}.jsonl"
        report = asyncio.run(
            codex_report(
                tmp_path, label="race", nested_mcp_tools=[tool], transcript_path=transcript
            )
        )
        return Path(report["home"]).parent

    homes = run_together(
        *[lambda: call("lint")] * 4,
        *[lambda: call("sim")] * 4,
    )

    lint, sim = set(homes[:4]), set(homes[4:])
    assert len(lint) == 1
    assert len(sim) == 1
    assert lint != sim
    assert sorted(marked) == sorted(Path(h) for h in (*lint, *sim))
    identity = capture_process_identity(os.getpid())
    assert identity is not None
    for home, tool in ((*lint, "lint"), (*sim, "sim")):
        marker = json.loads((Path(home) / ".booley-owner.json").read_text(encoding="utf-8"))
        assert marker["owner"] == identity.to_payload()
        config = tomllib.loads((Path(home) / ".codex" / "config.toml").read_text(encoding="utf-8"))
        assert config["mcp_servers"]["booley"]["env"]["BOOLEY_NESTED_MCP_TOOLS"] == tool
