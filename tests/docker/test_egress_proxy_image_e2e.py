"""Opt-in containerized CONNECT proof for the packaged egress proxy."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid

import pytest


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def _candidate_image() -> str:
    image = os.environ.get("BOOLEY_EGRESS_PROXY_IMAGE")
    if image is None:
        pytest.skip("set BOOLEY_EGRESS_PROXY_IMAGE to run the packaged proxy proof")
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    if _docker("image", "inspect", image).returncode != 0:
        pytest.skip(f"{image} is not built")
    return image


@pytest.mark.slow()
def test_candidate_connect_streams_bytes_and_reports_stats_on_shutdown() -> None:
    image = _candidate_image()
    version = _docker("run", "--rm", "--entrypoint", "python3", image, "--version")
    _assert_ok(version)
    assert (version.stdout + version.stderr).strip() == "Python 3.14.7"

    unique = uuid.uuid4().hex[:12]
    network = f"booley-egress-e2e-{unique}"
    upstream = f"booley-egress-upstream-e2e-{unique}"
    proxy = f"booley-egress-proxy-e2e-{unique}"
    server = """\
import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((\"0.0.0.0\", 9443))
s.listen()
while True:
    connection, _ = s.accept()
    data = connection.recv(65536)
    connection.sendall(b\"reply:\" + data)
    connection.close()
"""
    client = """\
import socket
import time
for attempt in range(50):
    try:
        connection = socket.create_connection((\"proxy\", 8080), timeout=2)
        break
    except OSError:
        if attempt == 49:
            raise
        time.sleep(0.1)
connection.sendall(b\"CONNECT upstream:9443 HTTP/1.1\\r\\nHost: upstream:9443\\r\\n\\r\\n\")
response = b\"\"
while b\"\\r\\n\\r\\n\" not in response:
    response += connection.recv(4096)
assert response.startswith(b\"HTTP/1.1 200\"), response
connection.sendall(b\"stream-through-proxy\")
reply = connection.recv(65536)
assert reply == b\"reply:stream-through-proxy\", reply
print(reply.decode())
"""

    try:
        _assert_ok(_docker("network", "create", network))
        _assert_ok(
            _docker(
                "run",
                "-d",
                "--name",
                upstream,
                "--network",
                network,
                "--network-alias",
                "upstream",
                "--entrypoint",
                "python3",
                image,
                "-u",
                "-c",
                server,
            )
        )
        _assert_ok(
            _docker(
                "run",
                "-d",
                "--name",
                proxy,
                "--network",
                network,
                "--network-alias",
                "proxy",
                "-e",
                'PROXY_ALLOWLIST=["upstream"]',
                image,
            )
        )
        flowed = _docker(
            "run",
            "--rm",
            "--network",
            network,
            "--entrypoint",
            "python3",
            image,
            "-c",
            client,
        )
        _assert_ok(flowed)
        assert flowed.stdout.strip() == "reply:stream-through-proxy"

        stopped = _docker("stop", "--time", "5", proxy, timeout=15)
        _assert_ok(stopped)
        logs = _docker("logs", proxy)
        _assert_ok(logs)
        stats = json.loads(logs.stdout.strip().splitlines()[-1])
        assert stats == {"allowed": 1, "blocked": 0, "errors": 0, "blocked_log": []}
    finally:
        _docker("rm", "-f", proxy, upstream)
        _docker("network", "rm", network)
