#!/usr/bin/env python3
"""External model protocol fixture using the product's real bound evidence server."""

import json
import os
import select
import shutil
import subprocess
import sys
import time
import tomllib
import weakref
from pathlib import Path


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


BUFFERS = weakref.WeakKeyDictionary()


def receive(stream, deadline: float) -> dict:
    pending = BUFFERS.setdefault(stream, bytearray())
    while b"\n" not in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise TimeoutError("provider fixture protocol deadline")
        chunk = os.read(stream.fileno(), 65536)
        if not chunk or len(pending) + len(chunk) > 1024 * 1024:
            raise ValueError("missing or oversized protocol message")
        pending.extend(chunk)
    line, _, rest = pending.partition(b"\n")
    pending[:] = rest
    return json.loads(line)


class Evidence:
    """Minimal bounded JSON-RPC client, not a replacement evidence implementation."""

    def __init__(self, spec: dict, inherited: bool, root: Path):
        self.root = root
        self.environment = {**(os.environ if inherited else {}), **spec.get("env", {})}
        self.log = (root / "mcp-stderr.txt").open("w")
        self.child = subprocess.Popen(
            [spec["command"], *spec.get("args", [])],
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            bufsize=1,
        )
        self.serial = 0
        self.deadline = time.monotonic() + 90

    def request(self, method: str, params: dict) -> dict:
        self.serial += 1
        request = {"jsonrpc": "2.0", "id": self.serial, "method": method, "params": params}
        self.child.stdin.write(json.dumps(request) + "\n")
        self.child.stdin.flush()
        for _ in range(128):
            response = receive(self.child.stdout, self.deadline)
            if response.get("id") == self.serial:
                with (self.root / "mcp.jsonl").open("a") as log:
                    log.write(json.dumps({"request": request, "response": response}) + "\n")
                if "error" in response:
                    raise ValueError(response["error"])
                return response["result"]
        raise ValueError("too many unsolicited MCP messages")

    def start(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "qa-model-boundary", "version": "1"},
            },
        )
        self.child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.child.stdin.flush()
        tools = self.request("tools/list", {})["tools"]
        if [tool["name"] for tool in tools] != ["coverage_evidence"]:
            raise ValueError("Analyst tool inventory is not exactly coverage_evidence")

    def query(self, **query) -> dict:
        result = self.request("tools/call", {"name": "coverage_evidence", "arguments": query})
        text = "\n".join(
            item["text"] for item in result.get("content", []) if item.get("type") == "text"
        )
        document = json.loads(text)
        if result.get("isError") or document.get("error"):
            raise ValueError(document)
        return document

    def close(self) -> None:
        self.child.stdin.close()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=5)
        self.log.close()
        audit = self.environment.get("BOOLEY_COVERAGE_AUDIT")
        if audit and Path(audit).is_file():
            shutil.copyfile(audit, self.root / "actual-evidence-audit.json")


def advisory(evidence: Evidence, case: str) -> dict:
    evidence.query(view="overview")
    disposition = {"non-rtl": "unscored", "unscored": "unscored", "waived": "waived"}
    filters = {}
    if case in {"unreachable-proof", "missing-proof"}:
        filters["covered"] = False
    elif case == "observed-hit":
        filters["covered"] = True
    page = evidence.query(
        view="points", disposition=disposition.get(case, "eligible"), limit=2, **filters
    )
    points = page.get("points", [])
    if not points:
        raise ValueError("required candidate fixture point was not delivered")
    point = points[0]["point_ref"]
    result = {"hypotheses": [], "recommendations": [], "waiver_candidates": []}
    if case in {"invented-reference", "unknown"}:
        point = "point:999999999"
    if case == "invented-reference":
        result["hypotheses"] = [{"point_refs": [point], "explanation": "QA invalid reference"}]
    elif case in {"missing-fields", "malformed-json", "incomplete", "context-exhausted"}:
        return result
    else:
        reason = (
            "unreachable"
            if case in {"unreachable-proof", "missing-proof", "observed-hit"}
            else "excluded"
        )
        candidate = {
            "point_ref": point,
            "reason": "invalid" if case == "invalid-reason" else reason,
            "evidence": "" if case == "missing-evidence" else "Controlled QA screening input",
            "proof_reference": "proof/parity.log"
            if case in {"unreachable-proof", "observed-hit"}
            else "",
        }
        result["waiver_candidates"] = [candidate] * (2 if case == "duplicate" else 1)
    return result


def query_fault(evidence: Evidence, case: str) -> None:
    evidence.query(view="overview")
    if case == "bad-cursor":
        evidence.query(view="points", cursor="bad", limit=1)
    elif case == "undelivered":
        evidence.query(view="source", point_refs=["point:999999999"])
    elif case == "cross-campaign":
        foreign = Path(os.environ["QA_FOREIGN_CAMPAIGN"]).resolve(strict=True)
        evidence.query(view="overview", campaign=str(foreign))
    elif case == "cursor-filter":
        page = evidence.query(view="points", metric="toggle", limit=1)
        evidence.query(view="points", metric="line", cursor=page["next_cursor"], limit=1)
    elif case == "invalid-limit":
        evidence.query(view="points", limit=100000)
    else:
        for _ in range(256):
            evidence.query(view="points", limit=100)
        raise ValueError("fixed retrieval workload did not exhaust evidence budget")


def model_result(spec: dict, inherited: bool, root: Path, case: str) -> dict:
    evidence = Evidence(spec, inherited, root)
    try:
        evidence.start()
        if case in {
            "bad-cursor",
            "undelivered",
            "cross-campaign",
            "cursor-filter",
            "invalid-limit",
            "total-budget",
        }:
            query_fault(evidence, case)
            raise ValueError("expected query rejection did not occur")
        if case == "response-budget":
            evidence.query(view="overview")
            page = evidence.query(view="points", limit=100)
            (root / "legal-page.json").write_bytes(
                json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            return {"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        return advisory(evidence, case)
    finally:
        evidence.close()


def claude_prompt() -> None:
    deadline = time.monotonic() + 30
    for _ in range(32):
        value = receive(sys.stdin, deadline)
        if value["type"] == "control_request":
            emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": value["request_id"],
                        "response": {},
                    },
                }
            )
        elif value["type"] == "user":
            return
    raise ValueError("Claude user prompt not received")


def output(provider: str, case: str, result: dict, error: str | None) -> None:
    text = (
        "{" if case == "malformed-json" else json.dumps({} if case == "missing-fields" else result)
    )
    if case == "context-exhausted" and error is None:
        error = "maximum context length exceeded: QA controlled model context exhaustion"
    if case == "incomplete" and error is None:
        # Exit normally before the required final message/result, preserving partial output.
        if provider == "codex":
            emit({"type": "thread.started", "thread_id": "qa-incomplete"})
            emit(
                {
                    "type": "item.started",
                    "item": {"id": "partial", "type": "agent_message", "text": "{"},
                }
            )
        else:
            emit({"type": "system", "subtype": "init", "session_id": "qa-incomplete"})
        return
    if provider == "codex":
        emit({"type": "thread.started", "thread_id": "qa-provider-fixture"})
        if error:
            emit({"type": "turn.failed", "error": {"message": error}})
        else:
            emit(
                {
                    "type": "item.completed",
                    "item": {"id": "qa-result", "type": "agent_message", "text": text},
                }
            )
            emit(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
                }
            )
    else:
        value = {
            "type": "result",
            "subtype": "error_during_execution" if error else "success",
            "duration_ms": 0,
            "duration_api_ms": 0,
            "is_error": bool(error),
            "num_turns": 1,
            "session_id": "qa-provider-fixture",
            "result": error or text,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        if not error and case != "malformed-json":
            value["structured_output"] = {} if case == "missing-fields" else result
        emit(value)
    if error:
        raise SystemExit(1)


def main() -> None:
    root = Path(os.environ["QA_PROVIDER_ROOT"]).resolve(strict=True)
    case = os.environ["QA_PROVIDER_CASE"]
    provider = "claude" if "--mcp-config" in sys.argv else "codex"
    if provider == "claude":
        spec = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])["mcpServers"]["booley"]
        claude_prompt()
    else:
        if len(sys.stdin.buffer.read(16 * 1024 * 1024)) >= 16 * 1024 * 1024:
            raise ValueError("prompt ceiling")
        config = Path(os.environ["CODEX_HOME"]) / "config.toml"
        spec = tomllib.loads(config.read_text())["mcp_servers"]["booley"]
    try:
        result = model_result(spec, provider == "claude", root, case)
    except (ValueError, OSError, TimeoutError) as exc:
        (root / "boundary-error.json").write_text(json.dumps({"case": case, "error": str(exc)}))
        output(provider, case, {}, str(exc))
    else:
        output(provider, case, result, None)


if __name__ == "__main__":
    main()
