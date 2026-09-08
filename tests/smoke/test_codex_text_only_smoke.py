"""Pinned CLI transport probe against localhost; no paid model calls.

Run with BOOLEY_CODEX_TEXT_ONLY_BINARY=/path/to/pinned/codex. The private model
catalog is a synthetic literal; no real account metadata or credentials are used.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from booley.core.models import AgentCallParams
from booley.runtime.text_only_agent import prepare_codex_text_only


def _model():
    return {
        "slug": "coverage-test",
        "display_name": "Coverage test",
        "description": None,
        "supported_reasoning_levels": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1,
        "availability_nux": None,
        "upgrade": None,
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "experimental_supported_tools": ["clock"],
        "base_instructions": "Return the requested text.",
        "tool_mode": "code_mode_only",
        "node_repl_disabled": False,
        "supports_search_tool": True,
    }


def _response():
    message = {
        "type": "message",
        "id": "msg_test",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "{}", "annotations": []}],
    }
    return [
        {
            "type": "response.created",
            "response": {"id": "resp_test", "status": "in_progress", "output": []},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_test",
                "status": "completed",
                "output": [message],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        },
    ]


def _recording_handler(requests):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in _response():
                self.wfile.write(
                    ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                )

        def log_message(self, *_args):
            pass

    return Handler


@pytest.mark.parametrize("container_bypass", [False, True])
def test_pinned_codex_request_contains_no_tools(tmp_path, container_bypass):
    binary = os.environ.get("BOOLEY_CODEX_TEXT_ONLY_BINARY")
    if not binary:
        pytest.skip("Set BOOLEY_CODEX_TEXT_ONLY_BINARY to run the pinned CLI localhost probe")
    original = tmp_path / "original"
    original.mkdir()
    (original / "models_cache.json").write_text(json.dumps({"models": [_model()]}))
    work = tmp_path / "empty"
    work.mkdir()
    requests = []
    server = HTTPServer(("127.0.0.1", 0), _recording_handler(requests))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_probe(binary, work, original, server.server_port, container_bypass)
        assert result.returncode == 0, result.stderr + result.stdout
        assert len(requests) == 1
        assert not requests[0].get("tools")
        assert '"type":"turn.completed"' in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_probe(binary: str, work: Path, original: Path, port: int, bypass: bool):
    params = AgentCallParams(prompt="Return {}", model="coverage-test", cwd=work, text_only=True)
    command = [
        binary,
        "exec",
        "--json",
        "-m",
        params.model,
        "-C",
        str(work),
        "-c",
        'model_provider="mock"',
        "-c",
        'model_providers.mock.name="Mock"',
        "-c",
        f'model_providers.mock.base_url="http://127.0.0.1:{port}/v1"',
        "-c",
        'model_providers.mock.wire_api="responses"',
        "-c",
        "model_providers.mock.requires_openai_auth=false",
    ]
    if bypass:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command, environment = prepare_codex_text_only(
        [*command, "-"], params, {**os.environ, "CODEX_HOME": str(original)}
    )
    return subprocess.run(
        command,
        input=params.prompt,
        text=True,
        capture_output=True,
        env=environment,
        timeout=45,
        cwd=work,
        check=False,
    )
