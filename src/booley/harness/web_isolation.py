"""Validate provider-side web-MCP-tool policy in the Session Runtime image."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

CODEX_POLICY = Path("etc/codex/requirements.toml")
CLAUDE_POLICY = Path("etc/claude-code/managed-settings.json")
CLAUDE_WEB_CAPABILITIES = frozenset({"WebFetch", "WebSearch"})


def policy_error(root: Path = Path("/")) -> str | None:
    """Return the first missing/malformed web-isolation policy, else ``None``."""
    codex_path = root / CODEX_POLICY
    try:
        codex = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return f"Codex web policy unreadable at {codex_path}: {exc}"
    if codex.get("allowed_web_search_modes") != []:
        return f"Codex web search is not forced disabled by {codex_path}"

    claude_path = root / CLAUDE_POLICY
    try:
        claude = json.loads(claude_path.read_text(encoding="utf-8"))
        denied = set(claude.get("permissions", {}).get("deny", []))
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        return f"Claude web policy unreadable at {claude_path}: {exc}"
    missing = sorted(CLAUDE_WEB_CAPABILITIES - denied)
    if missing:
        return f"Claude web policy at {claude_path} does not deny: {', '.join(missing)}"
    return None


def main() -> int:
    """CLI probe used by Doctor inside a selected sandbox image."""
    error = policy_error()
    if error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
