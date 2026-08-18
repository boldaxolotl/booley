"""Entry point for containerized egress proxy.

Runs the EgressProxy inside a Docker container. Listens on port 8080
(or PROXY_PORT env var). Allowlist configurable via PROXY_ALLOWLIST
env var (JSON array of domain strings).

On SIGTERM (docker stop), dumps final stats as JSON to stdout.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading

from egress_proxy import EgressProxy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def main() -> None:
    raw_port = os.environ.get("PROXY_PORT", "8080")
    try:
        port = int(raw_port)
    except ValueError:
        sys.exit(f"ERROR: PROXY_PORT must be an integer, got {raw_port!r}")

    allowlist_json = os.environ.get("PROXY_ALLOWLIST")
    allowlist = None
    if allowlist_json:
        try:
            allowlist = json.loads(allowlist_json)
        except json.JSONDecodeError as exc:
            sys.exit(f"ERROR: PROXY_ALLOWLIST is not valid JSON: {exc}")
        if not isinstance(allowlist, list):
            sys.exit(
                f"ERROR: PROXY_ALLOWLIST must be a JSON array, got {type(allowlist).__name__}"
            )
        # Every element must be a string: a non-string domain would later
        # reach allowed.lower() in EgressProxy._is_allowed and raise
        # AttributeError deep in the request path. Reject at this boundary.
        # NB: collect into a list rather than next(..., None) — a JSON ``null``
        # element *is* Python ``None``, which would collide with a ``None``
        # not-found sentinel and slip past the guard.
        bad = [d for d in allowlist if not isinstance(d, str)]
        if bad:
            sys.exit(
                f"ERROR: PROXY_ALLOWLIST entries must be strings, "
                f"got {type(bad[0]).__name__}: {bad[0]!r}"
            )

    proxy = EgressProxy(port=port, host="0.0.0.0", allowlist=allowlist)
    proxy.start()
    logger.info("Proxy ready on port %d", proxy.port)

    stop = threading.Event()

    def handle_signal(*_):
        logger.info("Shutting down...")
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    stop.wait()

    proxy.stop()
    # Print final stats as JSON to stdout for easy parsing by the host
    print(
        json.dumps(
            {
                "allowed": proxy.stats.allowed,
                "blocked": proxy.stats.blocked,
                "errors": proxy.stats.errors,
                "blocked_log": proxy.stats.blocked_log[:50],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
