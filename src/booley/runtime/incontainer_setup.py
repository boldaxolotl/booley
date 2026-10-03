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
   would violate Stealth Mode). The target app comes from ``BOOLEY_AGENT_APP``.
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
import tomllib
from pathlib import Path
from typing import Literal, NamedTuple

from booley.runtime import auth_token
from booley.runtime.mcp_config import HTTP_ENDPOINT_PATH, http_port

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
    # the Codex tool_timeout_sec below.
    return {"type": "http", "url": http_url(), "timeout": _TOOL_TIMEOUT_SEC * 1000}


def upsert_claude(path: Path) -> bool:
    """Ensure ``mcpServers.booley`` exists in *path*. Returns True if changed."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}

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

    target.mkdir(parents=True, exist_ok=True)

    # The skills dir now persists across rebuilds (named volume), so a skill
    # renamed or removed in a newer image leaves a dangling link. Prune dead
    # links first so the agent isn't offered a skill it can no longer read.
    for child in target.iterdir():
        if child.is_symlink() and not child.exists():
            try:
                child.unlink()
            except OSError:
                continue

    linked = 0
    for skill_dir in sorted(src.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        link = target / skill_dir.name
        # ``exists()`` follows symlinks; also guard ``is_symlink`` so a dangling
        # link (stale image rebuild) isn't re-created on top of itself.
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(skill_dir)
            linked += 1
        except OSError:
            continue
    return linked


def deploy_host_skills(app: str, home: Path | None = None) -> int:
    """Symlink the user's mounted HOST skills into the dir *app* discovers.

    The devcontainer spec binds each host skill read-only under
    ``~/.booley-host-skills/<name>`` (see ``[sandbox] mount_host_skills``); this
    links them into the same skills dir as the built-ins. Runs AFTER
    :func:`deploy_skills`, so a built-in of the same name is already present and
    an ``exists()`` skip lets it win the name clash. Idempotent, prunes dangling
    links from a removed/renamed host skill, and skips individual failures.
    Returns the number of host skills newly linked.
    """
    target = skills_target_dir(app, home)
    if target is None:
        return 0
    sidecar = (home or _agent_home()) / _HOST_SKILLS_SIDECAR
    if not sidecar.is_dir():
        return 0

    target.mkdir(parents=True, exist_ok=True)

    # Drop links to a host skill that is no longer mounted (renamed/removed on
    # the host, or mount_host_skills turned off) before re-linking the rest.
    for child in target.iterdir():
        if child.is_symlink() and not child.exists():
            try:
                child.unlink()
            except OSError:
                continue

    linked = 0
    for skill_dir in sorted(sidecar.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        link = target / skill_dir.name
        # A built-in (deployed first) or a prior host link of this name wins;
        # guard is_symlink too so a dangling link isn't recreated on itself.
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(skill_dir)
            linked += 1
        except OSError:
            continue
    return linked


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


def _codex_data(existing: str, *, path: Path | None = None) -> dict:
    """Validate interactive config before any registration mutation."""
    try:
        parsed = tomllib.loads(existing)
        if "suppress_unstable_features_warning" in parsed and not isinstance(
            parsed["suppress_unstable_features_warning"], bool
        ):
            raise ValueError("suppress_unstable_features_warning must be a boolean")
    except ValueError as exc:
        if path is None:
            raise
        raise ValueError(
            f"Invalid Codex config {path}: {exc}; repair this file and retry"
        ) from exc
    return parsed


def _read_codex_config(path: Path) -> str:
    """Read user TOML without normalizing unrelated line-ending bytes."""
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def _publish_codex_config(path: Path, content: str) -> None:
    """Publish validated TOML atomically, preserving existing permission bits."""
    _codex_data(content, path=path)
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


def _codex_entry_is_current(existing: str) -> bool:
    parsed = _codex_data(existing)
    servers = parsed.get("mcp_servers", {})
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    features = parsed.get("features", {})
    return (
        "suppress_unstable_features_warning" in parsed
        and entry == {"url": http_url(), "tool_timeout_sec": _TOOL_TIMEOUT_SEC}
        and isinstance(features, dict)
        and features.get("mcp_2026_07_28") is True
    )


def _toml_statements(existing: str) -> list[str]:
    """Use the TOML parser to find complete statements, including multiline values.

    Input is validated first. A partial multiline value cannot parse on its own,
    so headers/comments inside it never become independent editable statements.
    """
    tomllib.loads(existing)
    statements: list[str] = []
    pending = ""
    lines = existing.split("\n")
    for index, line in enumerate(lines):
        pending += line + ("\n" if index < len(lines) - 1 else "")
        try:
            tomllib.loads(pending)
        except tomllib.TOMLDecodeError:
            continue
        statements.append(pending)
        pending = ""
    if pending:
        raise ValueError("Incomplete validated TOML statement")
    return statements


def _toml_assignment(statement: str) -> tuple[str, str]:
    """Find the syntactic key boundary, including equals signs in quoted keys."""
    for index, character in enumerate(statement):
        if character != "=":
            continue
        try:
            key = tomllib.loads(statement[:index] + "=0")
        except tomllib.TOMLDecodeError:
            continue
        if key:
            return statement[:index], statement[index + 1 :]
    raise ValueError("Expected a validated TOML assignment")


def _toml_path(statement: str) -> tuple[str, ...]:
    """Decode syntactic keys without descending into inline-table values."""
    data = tomllib.loads(statement)
    if data and not statement.lstrip().startswith("["):
        spelling, _value = _toml_assignment(statement)
        data = tomllib.loads(spelling + "=0")
    path: list[str] = []
    while isinstance(data, dict) and len(data) == 1:
        key, data = next(iter(data.items()))
        path.append(key)
    return tuple(path)


class _TomlEntry(NamedTuple):
    text: str
    path: tuple[str, ...]
    header: bool


def _replace_toml_assignment(statement: str, value: str) -> str:
    """Replace only an assignment's value, retaining its spelling and comment."""
    spelling, old = _toml_assignment(statement)
    for index in range(1, len(old) + 1):
        tail = old[index:]
        if tail.strip() and not tail.lstrip().startswith("#"):
            continue
        try:
            tomllib.loads("value=" + old[:index])
        except tomllib.TOMLDecodeError:
            continue
        leading = old[: len(old) - len(old.lstrip())]
        return spelling + "=" + leading + value + tail
    raise ValueError("Expected a complete validated TOML value")


def _inline_table_members(body: str) -> list[str]:
    """Find member boundaries with the parser, not commas inside nested values."""
    members = []
    start = 0
    for index, character in enumerate(body):
        if character != ",":
            continue
        try:
            tomllib.loads("value={" + body[start:index] + "}")
        except tomllib.TOMLDecodeError:
            continue
        members.append(body[start:index])
        start = index + 1
    members.append(body[start:])
    return members


def _upsert_inline_table_setting(statement: str, key: str, value: str) -> str:
    """Edit one member of an inline table while preserving all other bytes."""
    spelling, raw = _toml_assignment(statement)
    opening = raw.index("{")
    for closing in range(opening + 1, len(raw)):
        if raw[closing] != "}":
            continue
        try:
            tomllib.loads("value=" + raw[: closing + 1])
        except tomllib.TOMLDecodeError:
            continue
        break
    else:
        raise ValueError("Expected a complete validated inline table")
    body = raw[opening + 1 : closing]
    members = _inline_table_members(body)
    for index, member in enumerate(members):
        if member.strip() and _toml_path(member) == (key,):
            members[index] = _replace_toml_assignment(member, value)
            break
    else:
        members.append(f" {key} = {value}")
        if not body.strip():
            members = members[1:]
    return spelling + "=" + raw[: opening + 1] + ",".join(members) + raw[closing:]


def _toml_entries(existing: str) -> list[_TomlEntry]:
    entries = []
    table: tuple[str, ...] = ()
    for statement in _toml_statements(existing):
        header = statement.lstrip().startswith("[")
        path = _toml_path(statement)
        if header:
            table = path
        entries.append(_TomlEntry(statement, path if header else table + path, header))
    return entries


def _strip_codex_table(existing: str) -> str:
    """Remove owned statements, preserving unrelated statements verbatim."""
    owned = ("mcp_servers", MCP_SERVER_NAME)
    return "".join(
        statement for statement, path, _header in _toml_entries(existing) if path[:2] != owned
    )


def _upsert_existing_codex_table_setting(
    existing: str, *, table: str, key: str, value: str
) -> str | None:
    """Update a scalar assignment or insert into its actual table boundary."""
    entries = _toml_entries(existing)
    target = (table, key)
    for index, (statement, path, header) in enumerate(entries):
        if path == target and not header:
            entries[index] = _TomlEntry(_replace_toml_assignment(statement, value), path, header)
            return "".join(item[0] for item in entries)
    for index, (statement, path, header) in enumerate(entries):
        if path == (table,) and not header and tomllib.loads(statement):
            updated = _upsert_inline_table_setting(statement, key, value)
            entries[index] = _TomlEntry(updated, path, header)
            return "".join(item.text for item in entries)
    for index, (_statement, path, header) in enumerate(entries):
        if path == (table,) and header:
            if not _statement.endswith("\n"):
                entries[index] = _TomlEntry(_statement + "\n", path, header)
            entries.insert(index + 1, _TomlEntry(f"{key} = {value}\n", target, False))
            return "".join(item[0] for item in entries)
    return None


def _upsert_codex_modern_mcp_feature(existing: str) -> str:
    """Keep deliberate MCP enablement, including root dotted feature syntax."""
    parsed = tomllib.loads(existing)
    if parsed.get("features", {}).get("mcp_2026_07_28") is True:
        return existing
    updated = _upsert_existing_codex_table_setting(
        existing, table="features", key="mcp_2026_07_28", value="true"
    )
    return updated if updated is not None else "features.mcp_2026_07_28 = true\n" + existing


def upsert_codex(path: Path) -> bool:
    """Migrate owned MCP settings and default warning preference without redumping."""
    existing = _read_codex_config(path)
    parsed = _codex_data(existing, path=path)
    if _codex_entry_is_current(existing):
        return False
    updated = existing
    if "suppress_unstable_features_warning" not in parsed:
        updated = "suppress_unstable_features_warning = true\n" + updated
    updated = _upsert_codex_modern_mcp_feature(updated)
    servers = parsed.get("mcp_servers", {})
    desired = {"url": http_url(), "tool_timeout_sec": _TOOL_TIMEOUT_SEC}
    if not isinstance(servers, dict) or servers.get(MCP_SERVER_NAME) != desired:
        updated = _strip_codex_table(updated)
        sep = "" if not updated or updated.endswith("\n") else "\n"
        updated += sep + codex_section()
    if not _codex_entry_is_current(updated):
        raise ValueError(f"Codex migration did not produce current settings: {path}")
    _publish_codex_config(path, updated)
    return True


def _upsert_codex_root_setting(existing: str, key: str, value: str) -> str:
    """Replace a root assignment or prepend safely before every TOML table."""
    target = tuple(key.split("."))
    entries = _toml_entries(existing)
    for index, (statement, path, header) in enumerate(entries):
        if path == target and not header:
            entries[index] = _TomlEntry(_replace_toml_assignment(statement, value), path, header)
            return "".join(item[0] for item in entries)
    return f"{key} = {value}\n" + existing


def _upsert_codex_full_access_notice(existing: str) -> str:
    """Acknowledge Codex's one-time full-access warning in existing TOML."""
    updated = _upsert_existing_codex_table_setting(
        existing,
        table="notice",
        key="hide_full_access_warning",
        value="true",
    )
    return (
        updated
        if updated is not None
        else _upsert_codex_root_setting(existing, "notice.hide_full_access_warning", "true")
    )


def _apply_codex_permission_mode(home: Path | None = None) -> str:
    """Pin Codex to its container-trusted, provider-web-disabled mode."""
    path = codex_config_path(home)
    existing = _read_codex_config(path)
    data = _codex_data(existing, path=path)
    notice = data.get("notice", {})
    if (
        data.get("approval_policy") == "never"
        and data.get("sandbox_mode") == "danger-full-access"
        and data.get("web_search") == "disabled"
        and isinstance(notice, dict)
        and notice.get("hide_full_access_warning") is True
    ):
        return "current"

    updated = _upsert_codex_root_setting(existing, "approval_policy", '"never"')
    updated = _upsert_codex_root_setting(updated, "sandbox_mode", '"danger-full-access"')
    updated = _upsert_codex_root_setting(updated, "web_search", '"disabled"')
    updated = _upsert_codex_full_access_notice(updated)
    _publish_codex_config(path, updated)
    return "written"


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


def _token_seed_path(app: str, home: Path | None = None) -> Path | None:
    """Where the spec mounts *app*'s stored rotation-free credential."""
    basename = auth_token.TOKEN_SEED_BASENAME.get(app)
    return None if basename is None else (home or _agent_home()) / basename


def _effective_token(app: str, home: Path | None = None) -> str | None:
    """The credential to apply: non-empty ambient env var first, seed second."""
    credential = auth_token.credential_for_app(app)
    if credential is None:
        return None
    ambient = (os.environ.get(credential.env_var) or "").strip()
    if ambient:
        return ambient
    seed = _token_seed_path(app, home)
    try:
        stored = seed.read_text(encoding="utf-8").strip() if seed else ""
    except (OSError, UnicodeDecodeError):
        stored = ""
    return stored or None


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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
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


def apply_stored_credential(app: str, home: Path | None = None) -> str:
    """Apply the ``booley auth`` credential for *app*. Returns a short status.

    ``"written"``/``"current"``/``"cleared"``/``"none"`` — see the per-app
    helpers. Unknown apps are a no-op.
    """
    token = _effective_token(app, home)
    if app == auth_token.APP_CLAUDE:
        return _apply_claude_credential(token, home)
    if app == auth_token.APP_CODEX:
        return _apply_codex_credential(token, home)
    return "none"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def register(app: str, *, home: Path | None = None) -> str:
    """Write the client registration for *app*. Returns a short status string."""
    if app == "claude":
        changed = upsert_claude(claude_config_path(home))
        linked = deploy_skills(app, home)
        host_linked = deploy_host_skills(app, home)
        cred = apply_stored_credential(app, home)
        # After the credential: both writers rewrite settings.json wholesale,
        # so the last one to read it must see the other's result.
        perm = _apply_claude_permission_mode(home)
        mcp = "written" if changed else "current"
        return f"claude:{mcp} skills:+{linked} host-skills:+{host_linked} cred:{cred} perm:{perm}"
    if app == "codex":
        changed = upsert_codex(codex_config_path(home))
        linked = deploy_skills(app, home)
        host_linked = deploy_host_skills(app, home)
        cred = apply_stored_credential(app, home)
        perm = _apply_codex_permission_mode(home)
        mcp = "written" if changed else "current"
        return f"codex:{mcp} skills:+{linked} host-skills:+{host_linked} cred:{cred} perm:{perm}"
    return "none"
