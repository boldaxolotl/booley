"""Loopback stand-in for the Anthropic Messages API, for probes that run the installed ``claude``.

``FakeModelApi`` answers each model request that offers tools with the next tool call of a
script, then with the text ``OK``. It records every request body, so a probe can read the tools
Claude offered the model and the results it sent back.

Standard library only: the Sandbox image's Python runs the probes without Booley installed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class _Server(ThreadingHTTPServer):
    def __init__(self, calls: Sequence[tuple[str, dict[str, Any]]]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.calls = calls
        self.requests: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.rstrip("/").endswith("count_tokens"):
            self._send(b'{"input_tokens":1}', "application/json")
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
    """Serve ``calls`` (tool name and arguments) to ``claude`` from a thread, as a context."""

    def __init__(self, calls: Sequence[tuple[str, dict[str, Any]]]) -> None:
        self._server = _Server(calls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests

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
