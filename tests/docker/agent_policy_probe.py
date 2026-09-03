"""Exercise the installed agent CLIs and their managed web-isolation policies."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

EXPECTED_CLAUDE = "2.1.259"
EXPECTED_CODEX = "0.153.1"
CLAUDE_WEB_TOOLS = {"WebFetch", "WebSearch"}
CODEX_POLICY_SOURCE = "/etc/codex/requirements.toml"
CANARY_TEXT = "booley-agent-policy-canary"


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise AssertionError(f"command failed ({result.returncode}): {command!r}\n{output}")
    return result


def _assert_versions(expected_node: str, expected_npm: str) -> dict[str, str]:
    versions = {
        "node": _run(["node", "--version"]).stdout.strip().removeprefix("v"),
        "npm": _run(["npm", "--version"]).stdout.strip(),
        "claude": _run(["claude", "--version"]).stdout.split()[0],
        "codex": _run(["codex", "--version"]).stdout.split()[1],
    }
    expected = {
        "node": expected_node,
        "npm": expected_npm,
        "claude": EXPECTED_CLAUDE,
        "codex": EXPECTED_CODEX,
    }
    assert versions == expected, f"runtime versions differ: {versions!r} != {expected!r}"
    return versions


def _assert_diagnostics() -> None:
    _run(["claude", "--help"])
    _run(["claude", "doctor"])
    _run(["codex", "--help"])
    codex = subprocess.run(
        ["codex", "doctor", "--json"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    report = json.loads(codex.stdout)
    assert report["codexVersion"] == EXPECTED_CODEX
    assert report["checks"]["config.load"]["summary"] == "config loaded"


def _walk_installed_tree(tree: dict[str, Any]) -> dict[str, str]:
    installed: dict[str, str] = {}
    pending = [tree]
    while pending:
        package = pending.pop()
        for name, dependency in package.get("dependencies", {}).items():
            if not dependency:
                continue
            version = dependency.get("version")
            assert isinstance(version, str), f"installed package has no version: {name}"
            installed[name] = version
            pending.append(dependency)
    return installed


def _assert_npm_tree() -> dict[str, str]:
    result = _run(["npm", "ls", "--prefix", "/opt/agent-clis", "--omit=dev", "--all", "--json"])
    installed = _walk_installed_tree(json.loads(result.stdout))
    lock = json.loads(Path("/opt/agent-clis/package-lock.json").read_text(encoding="utf-8"))
    locked = lock["packages"]
    assert installed["@anthropic-ai/claude-code"] == EXPECTED_CLAUDE
    assert installed["@openai/codex"] == EXPECTED_CODEX
    for name, version in installed.items():
        entry = locked[f"node_modules/{name}"]
        assert entry["version"] == version, f"installed {name} differs from package lock"
        assert entry["integrity"].startswith("sha512-"), f"{name} has no locked integrity"
    return dict(sorted(installed.items()))


def _assert_sdk_uses_system_claude() -> str:
    script = """
import anyio
from claude_agent_sdk import ClaudeAgentOptions, query

async def main():
    try:
        async for _ in query(
            prompt="probe",
            options=ClaudeAgentOptions(),
        ):
            pass
    except Exception as exc:
        text = str(exc)
        assert "CLI not found" not in text and "bundled" not in text.lower(), text

anyio.run(main)
"""
    import claude_agent_sdk

    bundle = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    assert not bundle.exists(), f"SDK bundled CLI remains: {bundle}"
    assert Path("/usr/local/bin/claude").resolve().is_file()
    _run(["python", "-c", script], timeout=20)
    return str(Path("/usr/local/bin/claude").resolve())


def _assert_booley_configuration(root: Path) -> dict[str, str]:
    from booley.harness.web_isolation import policy_error
    from booley.runtime import incontainer_register

    statuses: dict[str, str] = {}
    for app in ("claude", "codex"):
        home = root / f"booley-{app}-home"
        statuses[app] = incontainer_register.register(app, home=home)
    codex = (root / "booley-codex-home" / ".codex" / "config.toml").read_text(encoding="utf-8")
    claude = json.loads(
        (root / "booley-claude-home" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert 'web_search = "disabled"' in codex
    assert set(claude["permissions"]["deny"]) >= CLAUDE_WEB_TOOLS
    assert policy_error() is None
    return statuses


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _message_start() -> bytes:
    message = {
        "id": "msg_policy_probe",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    return _sse_event("message_start", {"type": "message_start", "message": message})


def _tool_response(canary: Path) -> bytes:
    start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "toolu_policy_probe",
            "name": "Read",
            "input": {},
        },
    }
    delta = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "input_json_delta",
            "partial_json": json.dumps({"file_path": str(canary)}),
        },
    }
    return b"".join(
        [
            _message_start(),
            _sse_event("content_block_start", start),
            _sse_event("content_block_delta", delta),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        ]
    )


def _final_response() -> bytes:
    start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    delta = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "OK"},
    }
    return b"".join(
        [
            _message_start(),
            _sse_event("content_block_start", start),
            _sse_event("content_block_delta", delta),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        ]
    )


class _Provider(ThreadingHTTPServer):
    def __init__(self, canary: Path) -> None:
        super().__init__(("127.0.0.1", 0), _ProviderHandler)
        self.canary = canary
        self.requests: list[dict[str, Any]] = []


class _ProviderHandler(BaseHTTPRequestHandler):
    server: _Provider

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.rstrip("/").endswith("count_tokens"):
            self._send_json({"input_tokens": 1})
            return
        self.server.requests.append(body)
        has_result = CANARY_TEXT in json.dumps(body.get("messages", []))
        self._send_sse(_final_response() if has_result else _tool_response(self.server.canary))

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_sse(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _claude_environment(home: Path, provider: _Provider) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "ANTHROPIC_API_KEY": "policy-probe-placeholder",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{provider.server_port}",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        }
    )
    return env


def _contains_canary_result(requests: list[dict[str, Any]]) -> bool:
    return any(CANARY_TEXT in json.dumps(request.get("messages", [])) for request in requests)


def _run_claude_case(case: str, root: Path, extra_args: list[str]) -> list[str]:
    home = root / f"claude-home-{case}"
    project = root / f"claude-project-{case}"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    canary = project / "canary.txt"
    canary.write_text(CANARY_TEXT, encoding="utf-8")
    provider = _Provider(canary)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    command = [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        *extra_args,
        "Read canary.txt, then search the web and fetch https://example.com.",
    ]
    try:
        _run(command, cwd=project, env=_claude_environment(home, provider), timeout=30)
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
    assert provider.requests, f"Claude {case} sent no model request"
    tools = {tool["name"] for tool in provider.requests[0].get("tools", [])}
    assert "Read" in tools, f"Claude {case} omitted Read canary: {sorted(tools)}"
    assert tools.isdisjoint(CLAUDE_WEB_TOOLS), f"Claude {case} exposed web tools: {sorted(tools)}"
    assert _contains_canary_result(provider.requests), f"Claude {case} did not return Read output"
    return sorted(tools)


def _assert_claude_policy(root: Path) -> dict[str, list[str]]:
    allow = json.dumps({"permissions": {"allow": ["Read", "WebFetch", "WebSearch"]}})
    user_settings = root / "claude-home-user" / ".claude" / "settings.json"
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text(allow, encoding="utf-8")
    project_settings = root / "claude-project-project" / ".claude" / "settings.json"
    project_settings.parent.mkdir(parents=True)
    project_settings.write_text(allow, encoding="utf-8")
    cases = {
        "user": [],
        "project": [],
        "cli_allow": ["--allowedTools=Read,WebFetch,WebSearch"],
        "bypass": ["--dangerously-skip-permissions"],
        "web_prompt": [],
    }
    return {case: _run_claude_case(case, root, args) for case, args in cases.items()}


def _codex_warning(report: dict[str, Any]) -> str:
    details = report["checks"]["config.load"]["details"]
    warning = details.get("startup warning", "")
    assert "falling back to required value Disabled" in warning, warning
    assert CODEX_POLICY_SOURCE in warning, warning
    return warning


def _run_codex_case(case: str, root: Path, extra_args: list[str]) -> str:
    home = root / f"codex-home-{case}"
    project = root / f"codex-project-{case}"
    home.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    codex_home = home / ".codex"
    codex_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})
    result = subprocess.run(
        ["codex", *extra_args, "doctor", "--json"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    report = json.loads(result.stdout)
    assert report["codexVersion"] == EXPECTED_CODEX
    try:
        return _codex_warning(report)
    except AssertionError as exc:
        raise AssertionError(f"Codex {case} policy diagnostic was incomplete: {exc}") from exc


def _assert_codex_policy(root: Path) -> dict[str, str]:
    user_home = root / "codex-home-user" / ".codex"
    user_home.mkdir(parents=True)
    (user_home / "config.toml").write_text('web_search = "live"\n', encoding="utf-8")
    project_config = root / "codex-project-project" / ".codex" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text('web_search = "live"\n', encoding="utf-8")
    project_root = project_config.parents[1]
    _run(["git", "init", "-q"], cwd=project_root)
    project_home = root / "codex-home-project" / ".codex"
    project_home.mkdir(parents=True)
    (project_home / "config.toml").write_text(
        f'[projects."{project_root}"]\ntrust_level = "trusted"\n', encoding="utf-8"
    )
    cases = {
        "user": [],
        "project": [],
        "cli_config": ["-c", 'web_search="live"'],
        "search_flag": ["--search"],
        "danger_full_access": ["--dangerously-bypass-approvals-and-sandbox"],
    }
    return {case: _run_codex_case(case, root, args) for case, args in cases.items()}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-node", default="24.20.0")
    parser.add_argument("--expected-npm", default="11.19.0")
    parser.add_argument("--evidence", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    assert os.geteuid() != 0, "probe must run as the image's unprivileged agent user"
    with tempfile.TemporaryDirectory(prefix="booley-agent-policy-") as temporary:
        root = Path(temporary)
        evidence = {
            "versions": _assert_versions(args.expected_node, args.expected_npm),
            "installed_packages": _assert_npm_tree(),
            "sdk_cli": _assert_sdk_uses_system_claude(),
            "booley_configuration": _assert_booley_configuration(root),
            "codex_policy": _assert_codex_policy(root),
            "claude_tools": _assert_claude_policy(root),
        }
        _assert_diagnostics()
    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    if args.evidence is not None:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
