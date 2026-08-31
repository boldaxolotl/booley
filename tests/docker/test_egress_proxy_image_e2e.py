"""Opt-in containerized CONNECT proof for the packaged egress proxy."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest
from tests.sidecar_image_helpers import (
    assert_ok,
    assert_python_version,
    candidate_image,
    docker,
)

_UPSTREAM_SERVER = """\
import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.settimeout(10)
s.bind((\"0.0.0.0\", 9443))
s.listen()
connection, _ = s.accept()
data = connection.recv(65536)
connection.sendall(b\"reply:\" + data)
connection.close()
s.close()
"""
_CLIENT = """\
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
for _ in range(8):
    response += connection.recv(4096)
    if b\"\\r\\n\\r\\n\" in response:
        break
else:
    raise RuntimeError(\"proxy response headers were incomplete\")
assert response.startswith(b\"HTTP/1.1 200\"), response
connection.sendall(b\"stream-through-proxy\")
reply = connection.recv(65536)
assert reply == b\"reply:stream-through-proxy\", reply
print(reply.decode())
"""


@dataclass(frozen=True)
class _Resources:
    network: str
    upstream: str
    proxy: str

    @classmethod
    def create(cls) -> _Resources:
        unique = uuid.uuid4().hex[:12]
        return cls(
            network=f"booley-egress-e2e-{unique}",
            upstream=f"booley-egress-upstream-e2e-{unique}",
            proxy=f"booley-egress-proxy-e2e-{unique}",
        )


def _start_upstream(image: str, resources: _Resources) -> None:
    assert_ok(docker("network", "create", resources.network))
    assert_ok(
        docker(
            "run",
            "-d",
            "--name",
            resources.upstream,
            "--network",
            resources.network,
            "--network-alias",
            "upstream",
            "--entrypoint",
            "python3",
            image,
            "-u",
            "-c",
            _UPSTREAM_SERVER,
        )
    )


def _start_proxy(image: str, resources: _Resources) -> None:
    assert_ok(
        docker(
            "run",
            "-d",
            "--name",
            resources.proxy,
            "--network",
            resources.network,
            "--network-alias",
            "proxy",
            "-e",
            'PROXY_ALLOWLIST=["upstream"]',
            image,
        )
    )


def _exercise_proxy(image: str, resources: _Resources) -> None:
    flowed = docker(
        "run",
        "--rm",
        "--network",
        resources.network,
        "--entrypoint",
        "python3",
        image,
        "-c",
        _CLIENT,
    )
    assert_ok(flowed)
    assert flowed.stdout.strip() == "reply:stream-through-proxy"


def _assert_shutdown_stats(resources: _Resources) -> None:
    assert_ok(docker("stop", "--time", "5", resources.proxy, timeout=15))
    logs = docker("logs", resources.proxy)
    assert_ok(logs)
    stats = json.loads(logs.stdout.strip().splitlines()[-1])
    assert stats == {"allowed": 1, "blocked": 0, "errors": 0, "blocked_log": []}


@pytest.mark.slow()
def test_candidate_connect_streams_bytes_and_reports_stats_on_shutdown() -> None:
    image = candidate_image("BOOLEY_EGRESS_PROXY_IMAGE", "packaged proxy proof")
    assert_python_version(image)
    resources = _Resources.create()
    try:
        _start_upstream(image, resources)
        _start_proxy(image, resources)
        _exercise_proxy(image, resources)
        _assert_shutdown_stats(resources)
    finally:
        docker("rm", "-f", resources.proxy, resources.upstream)
        docker("network", "rm", resources.network)
