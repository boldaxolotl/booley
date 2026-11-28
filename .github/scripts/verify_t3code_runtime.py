"""Run the pinned T3 Code binary in the Sandbox image and judge what it prints (#4781).

CI runs this as the Sandbox user with no network inside ``bwave-smoke``. It starts
``t3 serve`` on loopback with the installed managed policy, then fails when the
policy is rejected, the server does not become ready, ``t3 policy show`` does not
report Booley's policy as in effect, or ``t3 auth pairing create`` stops returning a
link under the base URL that ``booley session up`` will print. The file named by
``--policy`` holds the expected values: ``organization``, ``limits`` and optional
``settings``, each a ``{"value", "locked"}`` entry as T3 Code's policy file writes it.

``verify_t3code_agent_tools.py`` reuses its helpers for the check of the tools T3 Code offers
Claude and Codex. Standard library only: the image's Python runs it without Booley installed.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import signal
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

_INSTALLED_POLICY = "/etc/t3code/policy.json"
_PAIRING_BASE_URL = "http://127.0.0.1:3780"
_READY = "T3 Code server is ready."
_COMMAND_TIMEOUT_S = 60
_HEADER = re.compile(r"Managed by (?P<organization>.+) \((?P<path>.+), fingerprint \w+\)\.")
_SETTING = re.compile(r"\s+(?P<key>\w+) = (?P<value>.+?)(?P<locked> \(locked\))?")
_LOADED = re.compile(r"The running server \(.*\) loaded this policy\.")


def fail(step: str, output: str) -> NoReturn:
    raise SystemExit(f"{step}\n{output}")


def run_t3(*args: str, timeout_s: float = _COMMAND_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["t3", *args], capture_output=True, text=True, timeout=timeout_s, check=False
    )


def command_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit status {result.returncode}\n{result.stdout}{result.stderr}"


def start_server(root: Path, log: Path) -> subprocess.Popen[bytes]:
    command = ["t3", "serve", "--host", "127.0.0.1", "--port", "3773"]
    with log.open("wb") as sink:
        return subprocess.Popen(
            [*command, "--base-dir", str(root / "home"), "--no-browser", str(root / "project")],
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def wait_until_ready(server: subprocess.Popen[bytes], log: Path, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and server.poll() is None:
        if _READY in log.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.2)
    status = server.poll()
    fail(
        f"T3 Code did not become ready (exit status {status})",
        log.read_text(encoding="utf-8", errors="replace"),
    )


def stop_server(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is None:
        os.killpg(server.pid, signal.SIGTERM)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait()


def _parse_show(text: str) -> dict[str, Any]:
    lines = text.strip().splitlines()
    header = _HEADER.fullmatch(lines[0]) if lines else None
    limits_text = text.partition("Limits:\n")[2]
    if header is None or not limits_text:
        fail("t3 policy show output is not in the form Booley reads", text)
    settings_block = text.partition("Settings:\n")[2].partition("\n\n")[0]
    settings = {
        found["key"]: [json.loads(found["value"]), bool(found["locked"])]
        for found in map(_SETTING.fullmatch, settings_block.splitlines())
        if found
    }
    limits, _end = json.JSONDecoder().raw_decode(limits_text)
    return {
        "organization": header["organization"],
        "path": header["path"],
        "settings": settings,
        "limits": limits,
        "status": lines[-1],
    }


def _json(value: object) -> str:
    """Return *value* as JSON, so ``False`` and ``0`` differ as they do on the wire."""
    return json.dumps(value, sort_keys=True)


def _differences(prefix: str, expected: dict[str, Any], shown: dict[str, Any]) -> list[str]:
    absent = "<absent>"
    return [
        f"{prefix}.{key}: expected {expected.get(key, absent)!r}, shown {shown.get(key, absent)!r}"
        for key in sorted(expected.keys() | shown.keys())
        if (key in expected) != (key in shown) or _json(expected[key]) != _json(shown[key])
    ]


def _check_policy_show(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    result = run_t3("policy", "show", "--base-dir", str(root / "home"))
    if result.returncode:
        fail("t3 policy show failed", command_output(result))
    shown = _parse_show(result.stdout)
    expected = {
        key: [setting["value"], setting["locked"]]
        for key, setting in policy.get("settings", {}).items()
    }
    problems = _differences("settings", expected, shown["settings"])
    problems += _differences("limits", policy["limits"], shown["limits"])
    if (shown["organization"], shown["path"]) != (policy["organization"], _INSTALLED_POLICY):
        problems.append(f"policy source: {shown['organization']} at {shown['path']}")
    if not _LOADED.fullmatch(shown["status"]):
        problems.append(f"the running server does not use the installed policy: {shown['status']}")
    if problems:
        fail("t3 policy show does not report Booley's policy in effect", "\n".join(problems))
    return shown


def _expires_in_future(value: object) -> bool:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) > datetime.now(UTC)
    except (TypeError, ValueError):
        return False


def _check_pairing(root: Path) -> dict[str, Any]:
    result = run_t3(
        *["auth", "pairing", "create", "--base-dir", str(root / "home"), "--ttl", "5m"],
        *["--base-url", _PAIRING_BASE_URL, "--json"],
    )
    if result.returncode:
        fail("t3 auth pairing create failed", command_output(result))
    try:
        pairing = json.loads(result.stdout)
    except ValueError:
        pairing = None
    if not isinstance(pairing, dict):
        fail(
            "t3 auth pairing create did not print a JSON object",
            "(output withheld: it may hold a token)",
        )
    link = re.escape(_PAIRING_BASE_URL) + r"/pair#token=\S+"
    problems = []
    if not re.fullmatch(link, str(pairing.get("pairUrl"))):
        problems.append(f"pairUrl is not {_PAIRING_BASE_URL}/pair#token=<token>")
    if not _expires_in_future(pairing.get("expiresAt")):
        problems.append("expiresAt is not a time in the future")
    if problems:
        fail("t3 auth pairing create is not in the form session up reads", "\n".join(problems))
    return {"keys": sorted(pairing), "expiresAt": pairing["expiresAt"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_policy = Path(__file__).parents[2] / "src/booley/data/docker/t3code-policy.json"
    parser.add_argument("--policy", type=Path, default=default_policy)
    parser.add_argument("--ready-timeout", type=float, default=120)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    check = run_t3("policy", "check", _INSTALLED_POLICY)
    if check.returncode:
        fail("t3 policy check rejected the installed policy", command_output(check))
    with tempfile.TemporaryDirectory(prefix="t3code-") as tmp:
        root = Path(tmp)
        (root / "project").mkdir()
        log = root / "serve.log"
        server = start_server(root, log)
        try:
            wait_until_ready(server, log, args.ready_timeout)
            shown = _check_policy_show(root, policy)
            pairing = _check_pairing(root)
            if server.poll() is not None:
                fail(
                    f"T3 Code exited during the checks (exit status {server.poll()})",
                    log.read_text(encoding="utf-8", errors="replace"),
                )
        finally:
            stop_server(server)
    evidence = {
        "user": pwd.getpwuid(os.geteuid()).pw_name,
        "interfaces": [name for _index, name in socket.if_nameindex()],
        "policy_show": shown,
        "pairing": pairing,
    }
    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if args.evidence:
        args.evidence.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
