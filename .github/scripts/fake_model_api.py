"""Loopback stand-in for the Anthropic Messages and OpenAI Responses APIs, for the CLI probes.

``FakeModelApi`` answers each Messages request that offers tools with the next tool call of a
script, then with the text ``OK``. A Responses request (``codex``) gets the next item of a second
script. It records every request body, so a probe can read the tools a CLI offered the model and
the results it sent back.

Standard library only: the Sandbox image's Python runs the probes without Booley installed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _response(block: dict[str, Any], delta: dict[str, Any], stop_reason: str) -> bytes:
    message = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    return b"".join(
        [
            _sse_event("message_start", {"type": "message_start", "message": message}),
            _sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": block},
            ),
            _sse_event(
                "content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        ]
    )


def _tool_response(index: int, name: str, arguments: dict[str, Any]) -> bytes:
    block = {"type": "tool_use", "id": f"toolu_probe_{index}", "name": name, "input": {}}
    delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
    return _response(block, delta, "tool_use")


def _final_response() -> bytes:
    block = {"type": "text", "text": ""}
    return _response(block, {"type": "text_delta", "text": "OK"}, "end_turn")


def tool_results(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ``tool_result`` blocks of a model request, in message order."""
    return [
        block
        for message in body.get("messages", [])
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def call_outputs(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the tool call outputs of a Responses request, in input order."""
    return [item for item in body.get("input", []) if item["type"].endswith("_call_output")]


def _responses_reply(items: Sequence[dict[str, Any]], done: int) -> bytes:
    if done < len(items):
        item = {**items[done], "id": f"item_probe_{done}", "call_id": f"call_probe_{done}"}
    else:
        text = {"type": "output_text", "text": "OK"}
        item = {"type": "message", "id": "msg_probe", "role": "assistant", "content": [text]}
    response = {"id": f"resp_probe_{done}"}
    usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {**response, "usage": usage}},
    ]
    return b"".join(_sse_event(event["type"], event) for event in events)


class _Server(ThreadingHTTPServer):
    def __init__(
        self,
        calls: Sequence[tuple[str, dict[str, Any]]],
        responses_calls: Sequence[dict[str, Any]],
    ) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.calls = calls
        self.responses_calls = responses_calls
        self.requests: list[dict[str, Any]] = []
        self.responses_requests: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.rstrip("/").endswith("count_tokens"):
            self._send(b'{"input_tokens":1}', "application/json")
            return
        if self.path.rstrip("/").endswith("/responses"):
            self.server.responses_requests.append(body)
            reply = _responses_reply(self.server.responses_calls, len(call_outputs(body)))
            self._send(reply, "text/event-stream")
            return
        self.server.requests.append(body)
        done = len(tool_results(body))
        if body.get("tools") and done < len(self.server.calls):
            name, arguments = self.server.calls[done]
            self._send(_tool_response(done, name, arguments), "text/event-stream")
        else:
            self._send(_final_response(), "text/event-stream")

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeModelApi:
    """Serve the scripts from a thread, as a context.

    ``calls`` (tool name and arguments) are for ``claude``; ``responses_calls`` (Responses
    output items without their IDs) are for ``codex``.
    """

    def __init__(
        self,
        calls: Sequence[tuple[str, dict[str, Any]]],
        responses_calls: Sequence[dict[str, Any]] = (),
    ) -> None:
        self._server = _Server(calls, responses_calls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests

    @property
    def responses_requests(self) -> list[dict[str, Any]]:
        return self._server.responses_requests

    def codex_config(self, home: Path) -> None:
        """Create the Codex home *home* whose model provider is this API."""
        home.mkdir()
        (home / "config.toml").write_text(
            'model_provider = "probe"\n[model_providers.probe]\nname = "probe"\n'
            f'base_url = "http://127.0.0.1:{self._server.server_port}/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )

    def claude_environment(self) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": "policy-probe-placeholder",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self._server.server_port}",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        }

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
