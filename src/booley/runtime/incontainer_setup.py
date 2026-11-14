"""Mechanisms for registering and starting Booley inside a container.

ADR 0018/0023: the MCP server lives inside the dev container and serves
Interactive Mode over **streamable HTTP on loopback**. This module, run from
the devcontainer ``postCreateCommand``/``postStartCommand`` as ``python -m
booley.harness.incontainer_register``, does two things on every container start:

1. **Ensures the HTTP server is running** (``ensure_http_server``). The hook
   re-runs on resume, so a devcontainer stop→start brings the endpoint back —
   the fix for the "``booley`` missing from ``/mcp`` after resume" failure of
   the old client-spawned stdio child, which no agent app ever re-spawns.
2. **Writes the client registration** — an HTTP URL entry — to the agent
   user's container-side config (NOT a tracked file under ``/work``, which
   would violate Stealth Mode). Every allowed Agent CLI is registered (``BOOLEY_AGENT_APP``
   and ``BOOLEY_AGENT_CLIS``; see ``booley.config.agent.sandbox_agent_clis``).
3. **Applies the rotation-free credential** stored by ``booley auth``, from
   the read-only sidecar the spec mounts it at (Claude: settings.json ``env``;
   Codex: ``auth.json``) — the delivery path that works for VS Code's "Reopen
   in Container", where ``${localEnv:...}`` cannot see the stored file.
4. **Pins the agent's no-prompt permission mode** because the hardened
   container is the security boundary. These settings live only in the
   per-project container home, never in the host's agent configuration.

Idempotent: a live server is left alone; an up-to-date registration is not
rewritten. Stale stdio-form registrations from older Booley versions are
migrated to the URL form.

Note: exact client config locations/schemas vary by app version; the writers
below target the common formats and are intentionally easy to adjust.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from booley.runtime import auth_token
from booley.runtime.mcp_config import (
    GATEWAY_PROVIDER_ID,
    HTTP_ENDPOINT_PATH,
    _codex_editor,
    gateway_provider_toml,
    http_port,
)

MCP_SERVER_NAME = "booley"
_TOOL_TIMEOUT_SEC = 7200

# How this module launches the shared per-container HTTP server.
_SERVER_CMD = [sys.executable, "-m", "booley.mcp.server", "--transport", "http"]
_SERVER_LOG_PATH = "/tmp/booley_mcp_http.log"
_SERVER_START_TIMEOUT_SECONDS = 20.0


def http_url() -> str:
    """The loopback URL agent apps connect to (shared with the server)."""
    return f"http://127.0.0.1:{http_port()}{HTTP_ENDPOINT_PATH}"


def _agent_home() -> Path:
    return Path(os.environ.get("HOME", "/home/agent"))


# ---------------------------------------------------------------------------
# HTTP server lifecycle — start it if this container doesn't have one yet
# ---------------------------------------------------------------------------


def _port_is_serving(port: int, *, timeout: float = 0.5) -> bool:
    """True if something accepts TCP connections on loopback *port*."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_http_server(
    *,
    mode: Literal["interactive"],
    timeout_seconds: float = _SERVER_START_TIMEOUT_SECONDS,
    log_path: str = _SERVER_LOG_PATH,
) -> str:
    """Start the HTTP MCP server unless one is already serving. Returns status.

    The server is detached (own session, no stdio inheritance) so it outlives
    this registrar and the ``postStartCommand`` shell; it then runs for the
    container's lifetime. Returns ``"running"`` (already up), ``"started"``,
    or ``"failed"`` — a failure is reported but never raises, so registration
    still proceeds (the client will surface the connection error).
    """
    port = http_port()
    if _port_is_serving(port):
        return "running"
    child_env = os.environ.copy()
    child_env["BOOLEY_MCP_MODE"] = mode
    for variable in (
        "BOOLEY_NESTED_AGENT",
        "BOOLEY_NESTED_MCP_TOOLS",
        "BOOLEY_MCP_TOOLS",
    ):
        child_env.pop(variable, None)
    try:
        with Path(log_path).open("ab") as log:
            proc = subprocess.Popen(
                _SERVER_CMD,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError as exc:
        print(f"booley mcp http server spawn failed: {exc}", file=sys.stderr)
        return "failed"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _port_is_serving(port):
            return "started"
        if proc.poll() is not None:  # died during startup; see the log
            print(
                f"booley mcp http server exited with {proc.returncode} (see {log_path})",
                file=sys.stderr,
            )
            return "failed"
        time.sleep(0.2)
    print(f"booley mcp http server not serving after {timeout_seconds:.0f}s", file=sys.stderr)
    return "failed"


# ---------------------------------------------------------------------------
# Claude Code — user-scoped ~/.claude.json mcpServers entry
# ---------------------------------------------------------------------------


def claude_config_path(home: Path | None = None) -> Path:
    return (home or _agent_home()) / ".claude.json"


def desired_claude_entry() -> dict:
    # "http" is Claude Code's streamable-HTTP transport type; localhost URLs
    # are supported at user scope. NO_PROXY in the devcontainer spec keeps
    # this off the egress proxy. "timeout" (ms) is the per-server MCP-tool-call
    # cap — without it Claude Code kills a call at 60s (measured on 2.1.205,
    # ADR 0027 amendment 2026-07-09), which is what forced 50s poll waits.
    # Belt-and-braces with the image-level MCP_TOOL_TIMEOUT ENV; same 2h as
    # the Codex tool_timeout_sec below. It also raises Claude Code's 300s
    # no-progress idle timeout for HTTP servers. It cannot stop Claude Code
    # from auto-backgrounding a long call (120s by default); the image ENV
    # CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS does that.
    return {"type": "http", "url": http_url(), "timeout": _TOOL_TIMEOUT_SEC * 1000}


def _read_json_object(path: Path, label: str) -> dict:
    """Read a user JSON object; missing or blank is ``{}``, anything invalid is refused.

    Registration writers rewrite these files wholesale, so an unreadable or
    non-object file must stop them before any write: replacing it with ``{}``
    would silently destroy the user's settings.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ValueError(_invalid_json_message(label, path, exc)) from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(_invalid_json_message(label, path, exc)) from exc
    if not isinstance(data, dict):
        raise ValueError(_invalid_json_message(label, path, "top level must be an object"))
    return data


def _invalid_json_message(label: str, path: Path, reason: object) -> str:
    return (
        f"Invalid {label} {path}: {reason}; repair or delete this file, then restart the Sandbox"
    )


def upsert_claude(path: Path) -> bool:
    """Ensure ``mcpServers.booley`` exists in *path*. Returns True if changed."""
    data = _read_json_object(path, "Claude config")
    servers = data.setdefault("mcpServers", {})
    desired = desired_claude_entry()
    if servers.get(MCP_SERVER_NAME) == desired:
        return False
    servers[MCP_SERVER_NAME] = desired
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Skill deployment — packaged skills into the agent's skills dir
# ---------------------------------------------------------------------------
#
# On the host, ``booley init`` Step 8 symlinks the packaged skills into the
# user's skills dirs. That deployment targets the *host* home and never reaches
# the dev container, so an interactive container session would see the MCP tools
# but none of the ``booley-*`` skills. Mirror Step 8 here, container side, so the
# agent discovers the skills on every container start.
#
# Per-app skills dir mirrors the host model (host skill reconciliation):
# Claude Code reads ``~/.claude/skills``; the Codex CLI reads the generic
# cross-agent ``~/.agents/skills``.
_SKILLS_REL = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".agents") / "skills",
}

# Sidecar where the devcontainer spec binds the user's HOST agent skills
# ([sandbox] mount_host_skills), one read-only child dir per skill. Kept in sync
# with devcontainer.HOST_SKILLS_SIDECAR; duplicated here so this in-container
# module has no import dependency on the host-side harness package.
_HOST_SKILLS_SIDECAR = Path(".booley-host-skills")


def skills_target_dir(app: str, home: Path | None = None) -> Path | None:
    """Return the container-side skills dir *app* reads, or ``None`` if unknown."""
    rel = _SKILLS_REL.get(app)
    return None if rel is None else (home or _agent_home()) / rel


def _link_skills(source: Path, target: Path) -> int:
    """Prune dangling links in *target*, then link each skill dir of *source* not already there.

    The skills dir persists across rebuilds (named volume), so a skill renamed,
    removed, or unmounted leaves a dangling link; pruning it first keeps the
    agent from being offered a skill it can no longer read. An existing entry
    (including a dangling link, guarded by ``is_symlink``) is left alone, so an
    earlier deployment wins a name clash and the routine stays idempotent.
    Individual link failures are skipped so one bad skill can't block the rest.
    Returns the number of skills newly linked.
    """
    target.mkdir(parents=True, exist_ok=True)

    for child in target.iterdir():
        if child.is_symlink() and not child.exists():
            try:
                child.unlink()
            except OSError:
                continue

    linked = 0
    for skill_dir in sorted(source.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        link = target / skill_dir.name
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(skill_dir)
            linked += 1
        except OSError:
            continue
    return linked


def deploy_skills(app: str, home: Path | None = None) -> int:
    """Symlink packaged Booley skills into the dir *app* discovers them from.

    Returns the number of skills newly linked. Existing links are left as-is
    (idempotent); individual failures are skipped so one bad skill can't block
    the rest.
    """
    from booley.runtime.paths import skills_dir

    target = skills_target_dir(app, home)
    if target is None:
        return 0
    src = skills_dir()
    if not src.is_dir():
        return 0
    return _link_skills(src, target)


def deploy_host_skills(app: str, home: Path | None = None) -> int:
    """Symlink the user's mounted HOST skills into the dir *app* discovers.

    The devcontainer spec binds each host skill read-only under
    ``~/.booley-host-skills/<name>`` (see ``[sandbox] mount_host_skills``); this
    links them into the same skills dir as the built-ins. Runs AFTER
    :func:`deploy_skills`, so a built-in of the same name is already present and
    the existing-entry skip lets it win the name clash. Idempotent, prunes dangling
    links from a removed/renamed host skill, and skips individual failures.
    Returns the number of host skills newly linked.
    """
    target = skills_target_dir(app, home)
    if target is None:
        return 0
    sidecar = (home or _agent_home()) / _HOST_SKILLS_SIDECAR
    if not sidecar.is_dir():
        return 0
    return _link_skills(sidecar, target)


# ---------------------------------------------------------------------------
# Codex — ~/.codex/config.toml [mcp_servers.booley] table
# ---------------------------------------------------------------------------


def codex_config_path(home: Path | None = None) -> Path:
    return (home or _agent_home()) / ".codex" / "config.toml"


def codex_section() -> str:
    # URL (streamable HTTP) entry; Codex supports these in config.toml with
    # no extra flags. Loopback, so no auth header is needed.
    return (
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f"url = {json.dumps(http_url())}\n"
        f"tool_timeout_sec = {_TOOL_TIMEOUT_SEC}\n"
    )


_INVALID_CODEX = "Invalid Codex config {}: {}; repair this file and retry"


def _validate_codex_shape(parsed):
    for table in ("features", "notice", "mcp_servers"):
        if table in parsed and not isinstance(parsed[table], dict):
            raise ValueError(f"{table} must be a table")
    _require_project_tables(parsed)
    if not isinstance(parsed.get("suppress_unstable_features_warning", True), bool):
        raise ValueError("suppress_unstable_features_warning must be a boolean")


def _parse_codex_config(path: Path, text: str | None = None):
    """The shared document step: parse once, check shape; returns ``(text, document)``."""
    editor = _codex_editor()
    if text is None:
        text = _read_codex_config(path)
    try:
        document = editor.parse(text)
        _validate_codex_shape(document.unwrap())
    except (ValueError, editor.exceptions.TOMLKitError) as exc:
        raise ValueError(_INVALID_CODEX.format(path, exc)) from exc
    return text, document


def _require_project_tables(parsed: dict) -> None:
    """Reject ``projects`` shapes Codex cannot use, before any write."""
    projects = parsed.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("projects must be a table")
    for key, entry in projects.items():
        if not isinstance(entry, dict):
            raise ValueError(f"projects.{key} must be a table")


def _read_codex_config(path: Path) -> str:
    """Read user TOML without normalizing unrelated line-ending bytes."""
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def _publish_codex_config(path: Path, content: str, *, validated: bool = False) -> None:
    """Publish TOML atomically, preserving existing permission bits.

    Validates *content* first unless the caller already did (``validated``).
    """
    if not validated:
        _parse_codex_config(path, content)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_if_changed(path: Path, existing: str, updated: str, *, validated: bool) -> bool:
    """Publish *updated* unless it equals *existing*; say whether it wrote."""
    if updated == existing:
        return False
    _publish_codex_config(path, updated, validated=validated)
    return True


def _set_codex_root_keys(document, values: dict) -> None:
    """Update root keys in place; insert missing ones first, so a trailing comment that
    introduces the next table stays attached to it."""
    position = 0
    prepend = bool(document.body)  # an empty document has nothing to insert before
    for key, value in values.items():
        if key in document or not prepend:
            document[key] = value
        else:
            document._insert_at(position, key, value)
            position += 1


def _set_codex_table_key(parent, table: str, key: str, value) -> None:
    """Set *key* in *parent*'s *table* in its own form: section, dotted or inline.

    A new key in a section goes first, so it never lands after a comment that
    introduces the next table.
    """
    editor = _codex_editor()
    if table not in parent:
        parent[table] = editor.table()
    container = parent[table]
    if isinstance(container, editor.container.OutOfOrderTableProxy) and key not in container:
        # A split table: its one ``[table]`` header section takes a new key, first.
        container = next((s for s in container._tables if not s.is_super_table()), container)
    flat = isinstance(container, (editor.items.InlineTable, editor.container.OutOfOrderTableProxy))
    if key in container or flat or not container.value.body:
        container[key] = value
    else:
        container.value._insert_at(0, editor.key(key), editor.item(value))


def _append_codex_tables(document, text: str) -> None:
    """Append Booley's own *text* tables after every user table and comment."""
    for key, value in _codex_editor().parse(text).body:
        if key is not None:
            document.append(key, value)


def _prepare_codex_mcp(document) -> None:
    """Default the warning preference, enable MCP and replace a stale owned server."""
    data = document.unwrap()
    if "suppress_unstable_features_warning" not in data:
        _set_codex_root_keys(document, {"suppress_unstable_features_warning": True})
    if data.get("features", {}).get("mcp_2026_07_28") is not True:
        _set_codex_table_key(document, "features", "mcp_2026_07_28", True)
    if data.get("mcp_servers", {}).get(MCP_SERVER_NAME) == {
        "url": http_url(),
        "tool_timeout_sec": _TOOL_TIMEOUT_SEC,
    }:
        return
    servers = document.get("mcp_servers", {})
    if isinstance(servers, _codex_editor().items.InlineTable):
        raise ValueError("an inline mcp_servers table cannot take the Booley server")
    servers.pop(MCP_SERVER_NAME, None)
    _append_codex_tables(document, codex_section())


def _prepare_codex_permissions(document) -> None:
    """Pin the no-prompt permission settings in the document; keep unrelated trivia."""
    desired = {
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        "web_search": "disabled",
    }
    data = document.unwrap()
    _set_codex_root_keys(document, {k: v for k, v in desired.items() if data.get(k) != v})
    if data.get("notice", {}).get("hide_full_access_warning") is not True:
        _set_codex_table_key(document, "notice", "hide_full_access_warning", True)


def _prepare_codex_registration(path: Path) -> tuple[str, str, str]:
    """The config before, after the MCP edit, and after permissions; all checked up front."""
    existing, document = _parse_codex_config(path)
    try:
        _prepare_codex_mcp(document)
        mcp_only = document.as_string()
        _prepare_codex_permissions(document)
        combined = document.as_string()
    except (ValueError, _codex_editor().exceptions.TOMLKitError) as exc:
        raise ValueError(_INVALID_CODEX.format(path, exc)) from exc
    parsed = {
        text: _parse_codex_config(path, text)[1] for text in {mcp_only, combined} - {existing}
    }
    if mcp_only in parsed:
        data = parsed[mcp_only].unwrap()
        entry = data.get("mcp_servers", {}).get(MCP_SERVER_NAME)
        current = (
            entry == {"url": http_url(), "tool_timeout_sec": _TOOL_TIMEOUT_SEC}
            and "suppress_unstable_features_warning" in data
            and data.get("features", {}).get("mcp_2026_07_28") is True
        )
        if not current:
            raise ValueError(f"Codex migration did not produce current settings: {path}")
    return existing, mcp_only, combined


# ---------------------------------------------------------------------------
# Rotation-free credential (`booley auth`) — apply the mounted seed
# ---------------------------------------------------------------------------
#
# The devcontainer spec bind-mounts the credential stored by `booley auth`
# read-only at a home sidecar (see devcontainer._APP_TOKEN_SEED_TARGET). It
# must be applied HERE, container-side, because the spec's other delivery
# route — a `${localEnv:...}` remoteEnv reference — is invisible to VS Code's
# "Reopen in Container": VS Code resolves localEnv against its own process
# env, not the shell `booley auth` ran in. This hook runs on every container
# start, so a re-minted credential propagates on the next start.
#
# Precedence (the export escape hatch): a NON-EMPTY ambient env var wins over
# the mounted seed. Claude Code applies settings.json `env` ON TOP of the
# process env (verified against the 2.1.207 CLI: settings env is
# Object.assign-ed over process.env, and every credential check is a
# truthiness test, so VS Code resolving an absent localEnv to "" is treated
# as unset). Writing the ambient value when one is exported therefore keeps
# "explicit export wins" true under either precedence direction.


def claude_settings_path(home: Path | None = None) -> Path:
    return (home or _agent_home()) / ".claude" / "settings.json"


def codex_auth_path(home: Path | None = None) -> Path:
    return (home or _agent_home()) / ".codex" / "auth.json"


def _write_private_json(path: Path, data: dict) -> None:
    """Write *data* as JSON with mode 0600 from the first byte (it holds a secret)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 even if the file pre-existed


def _apply_claude_credential(token: str | None, home: Path | None = None) -> str:
    """Sync ``settings.json`` ``env.CLAUDE_CODE_OAUTH_TOKEN`` with *token*.

    Claude Code applies the settings ``env`` map to every session — CLI and VS
    Code extension alike — which makes it the one container-side location that
    reaches a "Reopen in Container" session. The entry is treated as
    Booley-managed: when no credential is available anymore (``booley auth
    --clear`` + rebuild), it is REMOVED, else the stale token would sit on the
    persistent ``~/.claude`` state volume overriding the freshly seeded
    subscription credentials forever.
    """
    path = claude_settings_path(home)
    data = _read_json_object(path, "Claude settings")
    env_map = data.get("env")
    if not isinstance(env_map, dict):
        env_map = {}
        data["env"] = env_map

    var = auth_token.CREDENTIALS[auth_token.APP_CLAUDE].env_var
    if token is None:
        if var not in env_map:
            return "none"
        del env_map[var]
        if not env_map:
            del data["env"]
        _write_private_json(path, data)
        return "cleared"
    if env_map.get(var) == token:
        return "current"
    env_map[var] = token
    _write_private_json(path, data)
    return "written"


def is_registrar_codex_key_file(path: Path) -> bool:
    """Whether *path* is exactly the single-key ``auth.json`` this module writes.

    Such a file is a copy of a stored or exported API key, not a Codex login,
    so it must not count as the visitor's credential on a later start.
    """
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    var = auth_token.CREDENTIALS[auth_token.APP_CODEX].env_var
    return isinstance(existing, dict) and set(existing) == {var}


def _apply_codex_credential(token: str | None, home: Path | None = None) -> str:
    """Sync ``~/.codex/auth.json`` with the stored API key *token*.

    Codex's only rotation-free credential is an API key, and ``auth.json`` is
    where Codex reads it. This runs AFTER the postStart creds-seed ``cp`` (the
    hooks share one command chain), so a stored key deliberately wins over the
    seeded subscription login — same precedence as Claude's env token over its
    mounted subscription credentials. Removal is shape-checked: only the exact
    single-key file this function writes is cleaned up, so a user's own
    in-container ``codex login`` output is never touched.
    """
    path = codex_auth_path(home)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None

    var = auth_token.CREDENTIALS[auth_token.APP_CODEX].env_var
    desired = {var: token}
    if token is None:
        # Booley's write is exactly {var: key}; anything else is not ours.
        if isinstance(existing, dict) and set(existing) == {var}:
            try:
                path.unlink()
            except OSError:
                return "none"
            return "cleared"
        return "none"
    if existing == desired:
        return "current"
    _write_private_json(path, desired)
    return "written"


# ---------------------------------------------------------------------------
# Permission mode — bypass by default, because the container IS the sandbox
# ---------------------------------------------------------------------------
#
# Claude Code only offers "bypass permissions" when the session was LAUNCHED in
# that mode: the shift+tab cycle steps DOWN out of it, never up into it
# (verified against the 2.1.218 CLI — the availability flag is literally
# "session not launched in bypassPermissions mode"). Left alone, an
# in-container session therefore tops out at "auto" and charges a prompt for
# every write inside a cap-dropped, no-new-privileges, egress-proxied
# container — the opposite of why Interactive Mode runs in a container at all
# (ADR 0028), and out of step with Booley's own headless agent sessions, which
# already run ``permission_mode="bypassPermissions"``.
#
# Two keys are needed, and both are written container-side only, onto the
# per-project ``~/.claude`` state volume — never onto the host's settings:
#
#   permissions.defaultMode            launch in bypassPermissions, which is
#                                      also what makes the mode selectable
#   skipDangerousModePermissionPrompt  skip the one-time "I accept the risk"
#                                      disclaimer, which a fresh state volume
#                                      would otherwise re-raise, and which a
#                                      non-TTY session cannot answer at all
#
# Booley-managed like the credential above: re-asserted on every container
# start. A session that wants prompts back steps down with shift+tab.
_CLAUDE_PERMISSION_MODE = "bypassPermissions"
_CLAUDE_WEB_CAPABILITIES = ("WebFetch", "WebSearch")


def _apply_claude_permission_mode(home: Path | None = None) -> str:
    """Pin the in-container Claude session's launch permission mode.

    Returns ``"written"`` or ``"current"``. Merges into ``settings.json``
    key-by-key: an ``env`` credential map and any user ``permissions.allow`` /
    ``deny`` rules alongside it survive untouched.
    """
    path = claude_settings_path(home)
    data = _read_json_object(path, "Claude settings")
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    denied = permissions.get("deny")
    if not isinstance(denied, list):
        denied = []
    required_denied = list(dict.fromkeys([*denied, *_CLAUDE_WEB_CAPABILITIES]))
    if (
        permissions.get("defaultMode") == _CLAUDE_PERMISSION_MODE
        and denied == required_denied
        and data.get("skipDangerousModePermissionPrompt") is True
    ):
        return "current"
    permissions["defaultMode"] = _CLAUDE_PERMISSION_MODE
    permissions["deny"] = required_denied
    data["permissions"] = permissions
    data["skipDangerousModePermissionPrompt"] = True
    # Private-mode write: the same file carries the OAuth token in ``env``.
    _write_private_json(path, data)
    return "written"


def _validate_persisted_policy(app: str, policy: str, home: Path | None) -> None:
    """Refuse incompatible user state; clear only recognized managed entries."""
    base = home or _agent_home()
    if app == auth_token.APP_CLAUDE:
        auth_token.inspect_config_seed(base / ".claude.json", policy)
        settings = claude_settings_path(home)
        if settings.exists():
            try:
                data = json.loads(settings.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("cannot inspect Claude settings; recreate the Sandbox") from exc
            if not isinstance(data, dict) or data.get("apiKeyHelper"):
                raise ValueError(
                    "Claude settings contain ambiguous credential state; recreate the Sandbox"
                )
            environment = data.get("env", {})
            if not isinstance(environment, dict) or (
                policy == "subscription" and environment.get("ANTHROPIC_API_KEY")
            ):
                raise ValueError(
                    f"Claude settings conflict with subscription policy: remove env.ANTHROPIC_API_KEY "
                    f"from {settings}, then restart"
                )
        if policy == "api_key":
            if (base / ".claude" / ".credentials.json").exists():
                raise ValueError(
                    "Claude login conflicts with API-key policy; recreate the policy-partitioned Sandbox"
                )
            _apply_claude_credential(None, home)
    elif app == auth_token.APP_CODEX:
        _validate_codex_persisted_policy(policy, home)


def _validate_codex_persisted_policy(policy: str, home: Path | None) -> None:
    path = codex_auth_path(home)
    if not path.exists():
        return
    if policy == "subscription":
        _apply_codex_credential(None, home)
        if path.exists() and not auth_token.subscription_available(
            auth_token.APP_CODEX, policy, path
        ):
            raise ValueError(
                f"Codex credential state conflicts with subscription policy: remove {path}, "
                "then run `codex login`"
            )
    elif policy == "api_key":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                "cannot inspect Codex credential state; recreate the Sandbox"
            ) from exc
        if not isinstance(data, dict) or set(data) != {"OPENAI_API_KEY"}:
            raise ValueError(
                "Codex login conflicts with API-key policy; recreate the policy-partitioned Sandbox"
            )


def apply_stored_credential(app: str, home: Path | None = None) -> str:
    """Apply the ``booley auth`` credential for *app*. Returns a short status.

    ``"written"``/``"current"``/``"cleared"``/``"none"`` — see the per-app
    helpers. Unknown apps are a no-op.
    """
    from booley.config.agent import sandbox_cli_policy
    from booley.config.gateway import GATEWAY_AUTH

    policy = sandbox_cli_policy(app) or "auto"
    if policy == GATEWAY_AUTH:  # already inside the container by definition
        # The gateway login is the provider written by _apply_codex_gateway;
        # there is no auth.json. Leave any Booley-managed credential copies
        # untouched and validate nothing.
        return "none"
    plan = auth_token.credential_plan(app, policy, home=home or _agent_home())
    credential = auth_token.credential_for_app(app)
    token = dict(plan.values).get(credential.env_var) if credential else None
    if not auth_token.defers_to_cli(policy):
        _validate_persisted_policy(app, policy, home)
    if app == auth_token.APP_CLAUDE:
        return _apply_claude_credential(token, home)
    if app == auth_token.APP_CODEX:
        return _apply_codex_credential(token, home)
    return "none"


# ---------------------------------------------------------------------------
# First-run state — no onboarding, login, folder-trust or auto-mode nudge prompt in the Sandbox
# ---------------------------------------------------------------------------
#
# A Sandbox without a seeded host config (a Codespace, or any host with no
# agent config to seed from) would otherwise greet the first ``claude`` /
# ``codex`` with a theme picker, a login picker, a "trust this folder?"
# dialog and, for Claude, the auto-mode default nudge. The Sandbox is already
# the isolation boundary (permission modes above), so trusting the Project's
# own directories adds no authority.
#
# Every write is set-if-absent: a value the user or a host seed already chose,
# including ``false`` / ``untrusted``, wins. Writes land in the per-Sandbox
# home, never in the host's agent configuration. Claude's custom-API-key
# approval dialog is deliberately NOT pre-answered: it is the billing consent.
_DEFAULT_CLAUDE_THEME = "dark"


def trust_keys(paths: Sequence[Path]) -> tuple[str, ...]:
    """Config keys under which the agent apps may look up each existing *path*.

    Both CLIs check the resolved path; the absolute as-spelled path is kept as
    well (a symlinked workspace or a ``/booley-project`` bind spelling).
    Ordered, de-duplicated, pure path work.
    """
    keys: dict[str, None] = {}
    for path in paths:
        if path.exists():
            keys[str(path.resolve())] = None
            keys[str(path.absolute())] = None
    return tuple(keys)


def _claude_credential_present(home: Path | None) -> bool:
    """Whether Claude would find a login credential, by the registrar's own inputs."""
    from booley.config.agent import sandbox_cli_policy

    policy = sandbox_cli_policy(auth_token.APP_CLAUDE) or "auto"
    credential = auth_token.effective_credential(
        auth_token.APP_CLAUDE, policy, home=home or _agent_home()
    )
    return credential is not None


def _trust_claude_projects(config: dict, trust: Sequence[str]) -> None:
    """Add ``hasTrustDialogAccepted`` to each *trust* project entry that lacks it."""
    if not trust:
        return
    projects = config.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("projects must be an object")
    for key in trust:
        entry = projects.setdefault(key, {})
        if not isinstance(entry, dict):
            raise ValueError(f"projects.{key} must be an object")
        entry.setdefault("hasTrustDialogAccepted", True)


def _preflight_claude(home: Path | None, trust: Sequence[str]) -> None:
    """Refuse a misshaped Claude config or settings file before any writer touches it."""
    config_path = claude_config_path(home)
    config = _read_json_object(config_path, "Claude config")
    _read_json_object(claude_settings_path(home), "Claude settings")
    try:
        if not isinstance(config.get("mcpServers", {}), dict):
            raise ValueError("mcpServers must be an object")
        _trust_claude_projects(json.loads(json.dumps(config)), trust)
    except ValueError as exc:
        raise ValueError(_invalid_json_message("Claude config", config_path, exc)) from exc


def _apply_claude_first_run(home: Path | None, trust: Sequence[str], *, onboard: bool) -> str:
    """Trust *trust* paths and mark the auto-mode nudge seen.

    With a credential, also skip onboarding and the theme picker.

    Returns ``"written"`` or ``"current"``. Both files are validated before
    either is written, so an invalid file stops registration untouched.
    """
    config_path = claude_config_path(home)
    settings_path = claude_settings_path(home)
    config = _read_json_object(config_path, "Claude config")
    settings = _read_json_object(settings_path, "Claude settings")
    before = json.dumps(config, sort_keys=True)
    try:
        _trust_claude_projects(config, trust)
    except ValueError as exc:
        raise ValueError(_invalid_json_message("Claude config", config_path, exc)) from exc
    # Independent of login (unlike onboarding), so not credential-gated.
    config.setdefault("hasSeenAutoDefaultNudge", True)
    if onboard:
        config.setdefault("hasCompletedOnboarding", True)
    status = "current"
    if json.dumps(config, sort_keys=True) != before:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        status = "written"
    if onboard and "theme" not in settings and "theme" not in config:
        settings["theme"] = _DEFAULT_CLAUDE_THEME
        # Private-mode write: the same file carries the OAuth token in ``env``.
        _write_private_json(settings_path, settings)
        status = "written"
    return status


def _trust_codex_project(document, key: str) -> None:
    """Edit *document* to trust *key*: in its existing entry, else in a new table."""
    projects = document.get("projects", {})
    if isinstance(projects, _codex_editor().items.InlineTable):
        raise ValueError("an inline projects table cannot take a new entry")
    if projects.get(key) is None:
        spelled = json.dumps(key, ensure_ascii=False)
        _append_codex_tables(document, f'[projects.{spelled}]\ntrust_level = "trusted"\n')
    else:
        _set_codex_table_key(projects, key, "trust_level", "trusted")


def _apply_codex_trust(home: Path | None, trust: Sequence[str]) -> str:
    """Add ``trust_level = "trusted"`` for each *trust* path without a ``trust_level``.

    Edits the parsed TOML in place, keeping comments and every other byte, and
    publishes once atomically. Each path's edit must parse and carry the trust;
    otherwise that path rolls back to the last good text and is skipped with a
    warning. Returns ``"written"`` or ``"current"``.
    """
    path = codex_config_path(home)
    existing, document = _parse_codex_config(path)
    good = existing
    for key in trust:
        if "trust_level" in document.get("projects", {}).get(key, {}):
            continue
        try:
            _trust_codex_project(document, key)
            checked = _parse_codex_config(path, document.as_string())[1].unwrap()
            trusted = checked.get("projects", {}).get(key, {}).get("trust_level") == "trusted"
        except (ValueError, _codex_editor().exceptions.TOMLKitError):
            trusted = False
        if trusted:
            good = document.as_string()
            continue
        document = _parse_codex_config(path, good)[1]
        print(
            f"warning: Codex trust for {key} skipped: the projects layout in {path} "
            'cannot take a new entry; add trust_level = "trusted" there by hand',
            file=sys.stderr,
        )
    return "written" if _publish_if_changed(path, existing, good, validated=True) else "current"


# ---------------------------------------------------------------------------
# Gateway provider — interactive Codex under BOOLEY_PRIMARY_AUTH=gateway (#2270)
# ---------------------------------------------------------------------------

# Codex may wait for the token helper this much longer than the helper waits for
# the user's token command, so the helper reports the command's stderr first.
_AUTH_HELPER_MARGIN_S = 15


def _desired_provider(provider_toml: str) -> dict:
    """The parsed ``booley_gateway`` table a registration should leave in the config."""
    return _codex_editor().parse(provider_toml).unwrap()["model_providers"][GATEWAY_PROVIDER_ID]


def _add_gateway_settings(document, provider_toml: str, model_pin: str | None) -> None:
    """Add the owned provider table and the set-if-absent keys to *document*."""
    from booley.config.gateway import GATEWAY_TOKEN_ENV

    data = document.unwrap()
    for name in ("model_providers", "shell_environment_policy"):
        if not isinstance(data.get(name, {}), dict):
            raise ValueError(f"{name} must be a table")
    providers = data.get("model_providers", {})
    if providers.get(GATEWAY_PROVIDER_ID) != _desired_provider(provider_toml):
        document.get("model_providers", {}).pop(GATEWAY_PROVIDER_ID, None)
        _append_codex_tables(document, provider_toml)
    root = {"model_provider": GATEWAY_PROVIDER_ID, "model": model_pin}
    _set_codex_root_keys(document, {k: v for k, v in root.items() if v and k not in data})
    if "fast_mode" not in data.get("features", {}):
        # The gateway rejects Codex's default priority tier (spike #2264).
        _set_codex_table_key(document, "features", "fast_mode", False)
    exclude = data.get("shell_environment_policy", {}).get("exclude")
    if exclude is None:
        _set_codex_table_key(document, "shell_environment_policy", "exclude", [GATEWAY_TOKEN_ENV])
    elif isinstance(exclude, list) and GATEWAY_TOKEN_ENV not in exclude:
        document["shell_environment_policy"]["exclude"].append(GATEWAY_TOKEN_ENV)
    # A non-list exclude is the user's own value: left alone, reported as skipped.


def _warn_gateway_overrides(data: dict) -> None:
    """Say so when a user's own Codex keys keep the gateway from working."""
    if data.get("model_provider") != GATEWAY_PROVIDER_ID:
        print(
            f"warning: Codex gateway provider {GATEWAY_PROVIDER_ID} is configured but not "
            f"selected: model_provider in your Codex config wins; set it to "
            f"{GATEWAY_PROVIDER_ID} to use the gateway",
            file=sys.stderr,
        )
    if data.get("features", {}).get("fast_mode") is True:
        print(
            "warning: features.fast_mode = true in your Codex config; the gateway rejects "
            "the priority tier, so set features.fast_mode = false",
            file=sys.stderr,
        )


def _gateway_inputs(path: Path) -> tuple[str, str | None] | None:
    """The provider TOML and model pin from the gateway environment; ``None`` if incomplete."""
    from booley.config.gateway import (
        GatewayConfigError,
        gateway_model_pin_from_env,
        gateway_settings_from_env,
    )
    from booley.runtime.gateway_token import COMMAND_TIMEOUT_S

    try:
        settings = gateway_settings_from_env()
        model_pin = gateway_model_pin_from_env().model
    except GatewayConfigError as exc:
        print(f"warning: Codex gateway provider skipped: {exc}", file=sys.stderr)
        return None
    provider_toml = gateway_provider_toml(
        settings.url,
        str(path.parent),
        timeout_ms=(COMMAND_TIMEOUT_S + _AUTH_HELPER_MARGIN_S) * 1000,
    )
    return provider_toml, model_pin


def _apply_codex_gateway(home: Path | None = None) -> str:
    """Add and select the ``booley_gateway`` provider in the user's Codex config.

    Only when Codex's own policy is ``gateway``: the Sandbox-wide
    ``BOOLEY_PRIMARY_AUTH=gateway`` of a hand-written spec, or ``codex=gateway`` in the
    ``BOOLEY_AGENT_CLIS`` record of an issued one, whichever CLI is the default app
    (not ``gateway_handoff_active()``: the registrar only runs inside a Sandbox, so
    it needs no container-marker test). Booley owns the provider table
    (a stale URL is replaced); ``model_provider``, ``model`` and
    ``features.fast_mode`` are written only when absent, so the user's keys
    win. Neither the token nor the token command reaches the file: ``auth``
    runs the helper, which runs the command on each Codex auth call.

    Returns ``off``, ``invalid`` (incomplete gateway environment), ``skipped``
    (a TOML layout that cannot take the entries), ``current`` or ``written``.
    """
    from booley.config import agent_app
    from booley.config.agent import sandbox_cli_policy
    from booley.config.gateway import GATEWAY_AUTH

    if sandbox_cli_policy("codex") != GATEWAY_AUTH:
        return _drop_codex_gateway(home) if agent_app.auto_choice() else "off"
    path = codex_config_path(home)
    inputs = _gateway_inputs(path)
    if inputs is None:
        return "invalid"
    provider_toml, model_pin = inputs
    existing, document = _parse_codex_config(path)
    try:
        _add_gateway_settings(document, provider_toml, model_pin)
        updated = document.as_string()
        data = _parse_codex_config(path, updated)[1].unwrap()
    except (ValueError, _codex_editor().exceptions.TOMLKitError):  # a layout we cannot extend
        data = None
    if data is None or not _gateway_entries_present(data, provider_toml):
        print(
            f"warning: Codex gateway provider skipped: the layout in {path} cannot take "
            "it; add the booley_gateway provider there by hand",
            file=sys.stderr,
        )
        return "skipped"
    _warn_gateway_overrides(data)
    return "written" if _publish_if_changed(path, existing, updated, validated=True) else "current"


def hide_gateway_from_claude(home: Path | None = None) -> str:
    """Blank the gateway settings in ``~/.claude/settings.json`` ``env``.

    Only in an issued Sandbox whose ``BOOLEY_AGENT_CLIS`` record puts Codex on the
    gateway (the gateway routes Codex, never Claude); a hand-written spec keeps today's
    Claude environment. Claude applies that map to every session, CLI and VS Code
    extension alike, so an empty value shadows whatever the container environment
    carries. Every key is blanked whether or not it is set at this start: VS Code may
    resolve ``localEnv`` again on a later attach without rerunning ``postStart``.
    ``BOOLEY_HIDDEN_GATEWAY=1`` tells Doctor "hidden" from "never configured".

    Returns ``off`` (not applicable) or ``hidden``. Raises ``ValueError`` for an
    unreadable settings file: the start must not leave the gateway visible to Claude.
    """
    from booley.config.agent import issued_codex_gateway
    from booley.config.gateway import GATEWAY_ENV_KEYS, HIDDEN_GATEWAY_ENV

    if not issued_codex_gateway():
        return "off"
    path = claude_settings_path(home)
    data = _read_json_object(path, "Claude settings")
    env_map = data.get("env")
    if not isinstance(env_map, dict):
        env_map = {}
        data["env"] = env_map
    wanted = {**dict.fromkeys(GATEWAY_ENV_KEYS, ""), HIDDEN_GATEWAY_ENV: "1"}
    if any(env_map.get(key) != value for key, value in wanted.items()):
        env_map.update(wanted)
        _write_private_json(path, data)
    return "hidden"


def _drop_codex_gateway(home: Path | None) -> str:
    """Remove Booley's gateway selection from the user's Codex config.

    For ``BOOLEY_AGENT_APP=auto`` only: a start that stopped using the gateway
    must not leave Codex routed through it. Drops the owned provider table,
    ``model_provider`` when it selects that provider, and ``model`` when it
    equals the current gateway model pin. ``features.fast_mode`` and the token
    exclusion stay: neither routes traffic and either may be the user's.

    Returns ``removed`` or ``off`` (nothing of Booley's to remove).
    """
    from booley.config.gateway import GatewayConfigError, gateway_model_pin_from_env

    path = codex_config_path(home)
    existing, document = _parse_codex_config(path)
    data = document.unwrap()
    providers = document.get("model_providers", {})
    if isinstance(providers, dict):
        providers.pop(GATEWAY_PROVIDER_ID, None)
    if data.get("model_provider") == GATEWAY_PROVIDER_ID:
        del document["model_provider"]
        try:
            pin = gateway_model_pin_from_env().model
        except GatewayConfigError:
            pin = None
        if pin and data.get("model") == pin:
            del document["model"]
    changed = _publish_if_changed(path, existing, document.as_string(), validated=False)
    return "removed" if changed else "off"


def _gateway_entries_present(data: dict, provider_toml: str) -> bool:
    """Whether parsed *data* carries the owned provider and the token exclusion."""
    from booley.config.gateway import GATEWAY_TOKEN_ENV

    providers = data.get("model_providers")
    policy = data.get("shell_environment_policy")
    provider = providers.get(GATEWAY_PROVIDER_ID) if isinstance(providers, dict) else None
    exclude = policy.get("exclude") if isinstance(policy, dict) else None
    return (
        provider == _desired_provider(provider_toml)
        and isinstance(exclude, list)
        and GATEWAY_TOKEN_ENV in exclude
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def register(app: str, *, home: Path | None = None, trust_paths: Sequence[str] = ()) -> str:
    """Write the client registration for *app*. Returns a short status string.

    *trust_paths* are the config keys (see :func:`trust_keys`) the app should
    trust without asking.
    """
    if app == "claude":
        _preflight_claude(home, trust_paths)
        changed = upsert_claude(claude_config_path(home))
        linked = deploy_skills(app, home)
        host_linked = deploy_host_skills(app, home)
        cred = apply_stored_credential(app, home)
        # After the credential: both writers rewrite settings.json wholesale,
        # so the last one to read it must see the other's result.
        perm = _apply_claude_permission_mode(home)
        first = _apply_claude_first_run(
            home, trust_paths, onboard=_claude_credential_present(home)
        )
        mcp = "written" if changed else "current"
        return (
            f"claude:{mcp} skills:+{linked} host-skills:+{host_linked} "
            f"cred:{cred} perm:{perm} first-run:{first}"
        )
    if app == "codex":
        path = codex_config_path(home)
        existing, mcp_only, combined = _prepare_codex_registration(path)
        changed = _publish_if_changed(path, existing, mcp_only, validated=True)
        linked = deploy_skills(app, home)
        host_linked = deploy_host_skills(app, home)
        cred = apply_stored_credential(app, home)
        perm = (
            "written"
            if _publish_if_changed(path, mcp_only, combined, validated=True)
            else "current"
        )
        first = _apply_codex_trust(home, trust_paths)
        gateway = _apply_codex_gateway(home)
        mcp = "written" if changed else "current"
        return (
            f"codex:{mcp} skills:+{linked} host-skills:+{host_linked} "
            f"cred:{cred} perm:{perm} first-run:{first} gateway:{gateway}"
        )
    return "none"
