"""A stand-in ``codex`` command for driving ``CodexBackend`` in tests (POSIX only).

The fake reads the whole prompt from stdin, then reports on stdout what it was
launched with: the selected provider and that provider's token command output,
credential variables, ``auth.json``, home, argv, model catalog and prompt size.
Tests observe the process boundary through ``codex_report`` instead of calling
Booley's spawn internals.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from booley.config import agent as agent_config
from booley.core.models import AgentCallParams
from booley.runtime import _codex_backend as cb

STALE_TOKEN = "SYNTHETIC_STALE_TOKEN"
# The login a test writes with ``write_codex_login`` (the real login Booley links).
LOGIN_BODY = '{"tokens":{"access_token":"SYNTHETIC_OLD"}}'

_SCRIPT = """\
import json, os, subprocess, sys, tomllib

home = os.environ["CODEX_HOME"]
with open(os.path.join(home, "config.toml"), "rb") as handle:
    config = tomllib.load(handle)
provider_id = config.get("model_provider")
provider = config.get("model_providers", {}).get(provider_id, {})
auth = provider.get("auth", {})
token_run = None
if auth:
    done = subprocess.run(
        [auth["command"], *auth.get("args", [])],
        cwd=auth.get("cwd"),
        capture_output=True,
        check=False,
        text=True,
    )
    token_run = {"stdout": done.stdout, "stderr": done.stderr, "code": done.returncode}
if os.environ.get("FAKE_CODEX_LAUNCHES"):
    with open(os.environ["FAKE_CODEX_LAUNCHES"], "a", encoding="utf-8") as launches:
        launches.write("launch\\n")
if token_run and token_run["stdout"] == "SYNTHETIC_STALE_TOKEN":
    # The shape of Codex 0.164 when the gateway answers 401 (after its own retries).
    failure = (
        "unexpected status 401 Unauthorized: invalid token, "
        "url: https://gateway.example/v1/responses"
    )
    print(json.dumps({"type": "error", "message": failure}))
    print(json.dumps({"type": "turn.failed", "error": {"message": failure}}))
    # Exits without reading stdin on purpose: a large prompt breaks the pipe.
    sys.exit(1)
# Like a real codex exec, read the whole prompt before doing any work.
prompt = sys.stdin.buffer.read()
catalog = None
for index, argument in enumerate(sys.argv):
    if argument == "-c" and sys.argv[index + 1].startswith("model_catalog_json="):
        catalog_path = json.loads(sys.argv[index + 1].split("=", 1)[1])
        with open(catalog_path, encoding="utf-8") as handle:
            catalog = json.load(handle)
report = {
    "model_provider": provider_id,
    "base_url": provider.get("base_url"),
    "fast_mode": config.get("features", {}).get("fast_mode"),
    "shell_exclude": config.get("shell_environment_policy", {}).get("exclude"),
    "auth": auth,
    "token_run": token_run,
    "auth_json_exists": os.path.exists(os.path.join(home, "auth.json")),
    "credentials_present": sorted(
        name
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "CLAUDE_CODE_OAUTH_TOKEN")
        if os.environ.get(name)
    ),
    "token_command_var_present": "BOOLEY_GATEWAY_TOKEN_COMMAND" in os.environ,
    "home": home,
    "argv": sys.argv[1:],
    "catalog": catalog,
    "prompt_bytes": len(prompt),
}
message = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}}
print(json.dumps(message))
"""


def install_fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake ``codex`` first on ``PATH`` and make the process a Sandbox-like one.

    Skips on Windows: the fake is a POSIX shebang script and nested Codex homes
    exist only in the Linux Sandbox.
    """
    if sys.platform == "win32":
        pytest.skip("the fake codex is a POSIX shebang script")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_bytes(f"#!{sys.executable}\n{_SCRIPT}".encode())
    fake.chmod(0o755)
    (tmp_path / "home").mkdir()

    # Nested homes are created under the system temp directory.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("BOOLEY_SLUG", "codex-fake")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    # In a Sandbox booley is installed; here it comes from the source tree.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    # The container marker is the host/Sandbox boundary: only a Sandbox creates nested homes.
    monkeypatch.setattr(cb, "_inside_container", lambda: True)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    # The nested-home cache is process-wide state that no public entry point empties.
    monkeypatch.setattr(cb, "_NESTED_HOMES", {})


def write_codex_login(tmp_path: Path, body: str = LOGIN_BODY) -> Path:
    """Write the real Codex login under the home ``install_fake_codex`` set."""
    login = tmp_path / "home" / ".codex" / "auth.json"
    login.parent.mkdir(parents=True, exist_ok=True)
    login.write_bytes(body.encode())
    return login


async def codex_report(
    cwd: Path, *, auth_mode: str = "subscription", **params: Any
) -> dict[str, Any]:
    """One ``CodexBackend.call`` against the fake; return the fake's decoded report."""
    params.setdefault("prompt", "p")
    params.setdefault("model", "gpt-5.5")
    params.setdefault("transcript_path", cwd / "transcript.jsonl")
    result = await cb.CodexBackend(auth_mode=auth_mode).call(AgentCallParams(cwd=cwd, **params))
    return json.loads(result.output)
