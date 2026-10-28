"""Codex provider ``auth.command`` helper: print the gateway bearer token.

Codex runs ``python -P -m booley.runtime.gateway_token`` and reads the token
from stdout. It comes from the environment Codex inherited, so it never touches
disk. This module is the seam for a refreshable token source (#2267).
"""

from __future__ import annotations

import sys

from booley.config.gateway import GatewayConfigError, gateway_settings_from_env


def main() -> int:
    """Write the validated token to stdout, or the config error to stderr."""
    try:
        settings = gateway_settings_from_env()
    except GatewayConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(settings.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
