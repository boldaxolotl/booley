"""Bounded, network-isolated credential transport with disposable host homes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import auth_token, session_issuance, session_runtime


@pytest.fixture(scope="module")
def transport_image():
    if not shutil.which("docker"):
        pytest.skip("Docker executable unavailable")
    found = subprocess.run(
        ["docker", "image", "inspect", "booley-sandbox:latest"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if found.returncode:
        pytest.skip("Docker daemon or local booley-sandbox:latest unavailable")
    image = f"booley-auth-transport-{uuid.uuid4().hex[:12]}:test"
    # The available image predates the canonical .booley alias; change only that link.
    built = subprocess.run(
        ["docker", "build", "--network", "none", "--pull=false", "-t", image, "-"],
        input="FROM booley-sandbox:latest\nUSER root\nRUN ln -sfn /work/.booley /booley-project\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    try:
        yield image
    finally:
        subprocess.run(
            ["docker", "image", "rm", image], capture_output=True, timeout=30, check=False
        )


@pytest.fixture
def transport_host(tmp_path, monkeypatch, transport_image):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "config").mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "BOOLEY_DIR",
        "BOOLEY_PRIMARY_AUTH",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(key, raising=False)
    launcher = home / ".local" / "bin" / "booley"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{launcher.parent}{os.pathsep}{os.environ['PATH']}")
    return tmp_path, transport_image


def _issued_transport_spec(root, image, app, policy, monkeypatch):
    project = root / "project"
    data = project / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text(f'[agent]\nprovider="{app}"\nauth="{policy}"\n')
    monkeypatch.setenv(auth_token.API_KEY_ENV[app], "SYNTHETIC_API_KEY")
    auth_token.store_token("SYNTHETIC_STORED", app)
    login = auth_token.subscription_creds_path(app)
    login.parent.mkdir(parents=True)
    login.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "SYNTHETIC_LOGIN"}}
            if app == "claude"
            else {"tokens": {"access_token": "SYNTHETIC_LOGIN"}, "OPENAI_API_KEY": None}
        )
    )
    if app == "claude":
        (Path.home() / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    **(
                        {"customApiKeyResponses": {"rejected": ["SYNTHETIC_API_KEY"]}}
                        if policy == "auto"
                        else {}
                    ),
                }
            )
        )
    sources = init_cmd._interactive_spec_sources(
        InitContext(project_root=project), init_cmd._NANGATE_PDK_NOT_REQUESTED, app, image
    )
    prepared = session_issuance.preview(project, sources.build)
    session_issuance.issue_prepared(project, prepared)
    session_issuance.inspect_prepared(project, prepared)
    return project, prepared.spec


def _probe_spec(spec, root):
    """Keep generated credential transport; replace execution authority for this probe."""
    spec = json.loads(json.dumps(spec))
    spec["mounts"] = [mount for mount in spec["mounts"] if "seed" in mount]
    src = Path(__file__).resolve().parents[2] / "src"
    spec["mounts"].append(f"source={src},target=/probe-src,type=bind,readonly")
    spec["runArgs"] = ["--network", "none"]
    spec["remoteUser"] = "root"
    spec["remoteEnv"].update({"HOME": "/home/agent", "PYTHONPATH": "/probe-src"})
    return spec


def _exec(name, *command):
    result = subprocess.run(
        ["docker", "exec", name, *command], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("app", ["claude", "codex"])
@pytest.mark.parametrize("policy", ["auto", "subscription", "api_key"])
@pytest.mark.parametrize("route", ["headless", "editor-contract"])
def test_actual_local_credential_transport(transport_host, monkeypatch, app, policy, route):
    root, image = transport_host
    project, issued = _issued_transport_spec(root, image, app, policy, monkeypatch)
    spec = _probe_spec(issued, root)
    name = f"booley-auth-probe-{uuid.uuid4().hex[:12]}"
    argv = session_runtime.docker_run_argv(spec, project, name)
    environment = argv.environment
    if route == "editor-contract":
        # VS Code resolves localEnv against its own process env, with no store fallback.
        environment = {
            **os.environ,
            **{key: os.environ.get(key) or "" for key in auth_token.CREDENTIAL_ENV_KEYS},
        }
        if app == "claude" and policy != "api_key":
            environment["CLAUDE_CODE_OAUTH_TOKEN"] = "SYNTHETIC_EDITOR_OAUTH"
    assert "SYNTHETIC" not in str(argv)
    try:
        result = subprocess.run(
            argv, env=environment, capture_output=True, text=True, timeout=30, check=False
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(
            _exec(
                name,
                "python3",
                "-c",
                'import os,json,pathlib;print(json.dumps({"key":bool(os.getenv("'
                + auth_token.API_KEY_ENV[app]
                + '")),"oauth":bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN")),"seeds":[p.name for p in pathlib.Path("/home/agent").glob(".*seed*")]}))',
            )
        )
        assert observed["key"] == (policy != "subscription")
        assert observed["oauth"] == (app == "claude" and policy != "api_key")
        assert (
            ".claude-creds-seed.json" in observed["seeds"]
            or ".codex-auth-seed.json" in observed["seeds"]
        ) == (policy != "api_key")
        fragments = spec["postCreateCommand"].split("; ")[:-1]
        if fragments:
            _exec(name, "sh", "-c", "; ".join(fragments))
        _exec(
            name,
            "python3",
            "-c",
            f'from pathlib import Path;from booley.runtime.incontainer_setup import apply_stored_credential;print(apply_stored_credential({app!r},Path("/home/agent")))',
        )
        if app == "claude" and policy != "subscription":
            auth = json.loads(_exec(name, "claude", "auth", "status", "--json"))
            assert auth["authMethod"] == "api_key"
            assert auth["apiKeySource"] == "ANTHROPIC_API_KEY"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
        subprocess.run(
            ["docker", "image", "rm", session_issuance.keeper_image(project)],
            capture_output=True,
            timeout=30,
            check=False,
        )


def test_editor_missing_api_key_cannot_fall_back(transport_host, monkeypatch):
    root, image = transport_host
    project, issued = _issued_transport_spec(root, image, "claude", "api_key", monkeypatch)
    spec = _probe_spec(issued, root)
    name = f"booley-auth-probe-{uuid.uuid4().hex[:12]}"
    argv = session_runtime.docker_run_argv(spec, project, name)
    environment = {**argv.environment, "ANTHROPIC_API_KEY": ""}
    try:
        result = subprocess.run(
            argv, env=environment, capture_output=True, text=True, timeout=30, check=False
        )
        assert result.returncode == 0, result.stderr
        _exec(
            name,
            "python3",
            "-c",
            'from pathlib import Path;from booley.runtime.incontainer_setup import apply_stored_credential;apply_stored_credential("claude",Path("/home/agent"))',
        )
        result = subprocess.run(
            ["docker", "exec", name, "claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert not json.loads(result.stdout)["loggedIn"]
        assert result.returncode != 0
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
        subprocess.run(
            ["docker", "image", "rm", session_issuance.keeper_image(project)],
            capture_output=True,
            timeout=30,
            check=False,
        )
