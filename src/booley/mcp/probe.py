"""Machine-readable health probe for the Interactive Mode MCP catalog."""

from __future__ import annotations

import json

from booley.mcp.server import build_mcp_probe_payload


def main() -> int:
    """Print one validated catalog payload for host Doctor."""
    print(json.dumps(build_mcp_probe_payload()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
