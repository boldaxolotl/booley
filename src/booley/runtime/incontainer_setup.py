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
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal, NamedTuple

from booley.runtime import auth_token
from booley.runtime.mcp_config import (
    GATEWAY_PROVIDER_ID,
    HTTP_ENDPOINT_PATH,
    _codex_toml_parser,
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
    # the Codex tool_timeout_sec below.
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


def _validate_codex_shape(parsed):
    for table in ("features", "notice"):
        if table in parsed and not isinstance(parsed[table], dict):
            raise ValueError(f"{table} must be a table")
    _require_project_tables(parsed)
    if "suppress_unstable_features_warning" in parsed and not isinstance(
        parsed["suppress_unstable_features_warning"], bool
    ):
        raise ValueError("suppress_unstable_features_warning must be a boolean")


def _codex_data(existing: str, *, path: Path | None = None) -> dict:
    """Validate interactive config before any registration mutation."""
    parser = _codex_toml_parser()
    try:
        parsed = parser.loads(existing)
        _validate_codex_shape(parsed)
    except ValueError as exc:
        if path is None:
            raise
        raise ValueError(
            f"Invalid Codex config {path}: {exc}; repair this file and retry"
        ) from exc
    return parsed


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


def _codex_entry_is_current(existing: str, *, path: Path | None = None) -> bool:
    parsed = _codex_data(existing, path=path)
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
    parser = _codex_toml_parser()
    parser.loads(existing)
    statements: list[str] = []
    pending = ""
    lines = existing.split("\n")
    for index, line in enumerate(lines):
        pending += line + ("\n" if index < len(lines) - 1 else "")
        try:
            parser.loads(pending)
        except parser.TOMLDecodeError:
            continue
        statements.append(pending)
        pending = ""
    if pending:
        raise ValueError("Incomplete validated TOML statement")
    return statements


def _toml_assignment(statement: str) -> tuple[str, str]:
    """Find the syntactic key boundary, including equals signs in quoted keys."""
    parser = _codex_toml_parser()
    for index, character in enumerate(statement):
        if character != "=":
            continue
        try:
            key = parser.loads(statement[:index] + "=0")
        except parser.TOMLDecodeError:
            continue
        if key:
            return statement[:index], statement[index + 1 :]
    raise ValueError("Expected a validated TOML assignment")


def _toml_path(statement: str) -> tuple[str, ...]:
    """Decode syntactic keys without descending into inline-table values."""
    parser = _codex_toml_parser()
    data = parser.loads(statement)
    if data and not statement.lstrip().startswith("["):
        spelling, _value = _toml_assignment(statement)
        data = parser.loads(spelling + "=0")
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
    parser = _codex_toml_parser()
    spelling, old = _toml_assignment(statement)
    for index in range(1, len(old) + 1):
        tail = old[index:]
        if tail.strip() and not tail.lstrip().startswith("#"):
            continue
        try:
            parser.loads("value=" + old[:index])
        except parser.TOMLDecodeError:
            continue
        leading = old[: len(old) - len(old.lstrip())]
        return spelling + "=" + leading + value + tail
    raise ValueError("Expected a complete validated TOML value")


def _inline_table_members(body: str) -> list[str]:
    """Find member boundaries with the parser, not commas inside nested values."""
    parser = _codex_toml_parser()
    members = []
    start = 0
    for index, character in enumerate(body):
        if character != ",":
            continue
        try:
            parser.loads("value={" + body[start:index] + "}")
        except parser.TOMLDecodeError:
            continue
        members.append(body[start:index])
        start = index + 1
    members.append(body[start:])
    return members


def _upsert_inline_table_setting(statement: str, key: str, value: str) -> str:
    """Edit one member of an inline table while preserving all other bytes."""
    parser = _codex_toml_parser()
    spelling, raw = _toml_assignment(statement)
    opening = raw.index("{")
    for closing in range(opening + 1, len(raw)):
        if raw[closing] != "}":
            continue
        try:
            parser.loads("value=" + raw[: closing + 1])
        except parser.TOMLDecodeError:
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
        has_members = bool(parser.loads("value={" + body + "\n}")["value"])
        separator = "," if has_members else ""
        updated = f" {key} = {value}" + separator + body
        return spelling + "=" + raw[: opening + 1] + updated + raw[closing:]
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


def _strip_owned_codex_table(existing: str, owned: tuple[str, ...]) -> str:
    """Remove statements under the *owned* table path, keeping every other byte."""
    return "".join(
        statement
        for statement, path, _header in _toml_entries(existing)
        if path[: len(owned)] != owned
    )


def _upsert_existing_codex_table_setting(
    existing: str, *, table: tuple[str, ...], key: str, value: str
) -> str | None:
    """Update a scalar assignment or insert into its actual table boundary."""
    parser = _codex_toml_parser()
    entries = _toml_entries(existing)
    target = (*table, key)
    for index, (statement, path, header) in enumerate(entries):
        if path == target and not header:
            entries[index] = _TomlEntry(_replace_toml_assignment(statement, value), path, header)
            return "".join(item[0] for item in entries)
    for index, (statement, path, header) in enumerate(entries):
        if path == table and not header and parser.loads(statement):
            updated = _upsert_inline_table_setting(statement, key, value)
            entries[index] = _TomlEntry(updated, path, header)
            return "".join(item.text for item in entries)
    for index, (_statement, path, header) in enumerate(entries):
        if path == table and header:
            if not _statement.endswith("\n"):
                entries[index] = _TomlEntry(_statement + "\n", path, header)
            entries.insert(index + 1, _TomlEntry(f"{key} = {value}\n", target, False))
            return "".join(item[0] for item in entries)
    return None


def _upsert_codex_modern_mcp_feature(existing: str) -> str:
    """Keep deliberate MCP enablement, including root dotted feature syntax."""
    parser = _codex_toml_parser()
    parsed = parser.loads(existing)
    if parsed.get("features", {}).get("mcp_2026_07_28") is True:
        return existing
    updated = _upsert_existing_codex_table_setting(
        existing, table=("features",), key="mcp_2026_07_28", value="true"
    )
    return updated if updated is not None else "features.mcp_2026_07_28 = true\n" + existing


def _prepare_codex_mcp(existing: str, path: Path) -> str:
    """Prepare the legacy MCP migration before publishing or applying credentials."""
    parsed = _codex_data(existing, path=path)
    if _codex_entry_is_current(existing):
        return existing
    updated = existing
    if "suppress_unstable_features_warning" not in parsed:
        updated = "suppress_unstable_features_warning = true\n" + updated
    updated = _upsert_codex_modern_mcp_feature(updated)
    servers = parsed.get("mcp_servers", {})
    desired = {"url": http_url(), "tool_timeout_sec": _TOOL_TIMEOUT_SEC}
    if not isinstance(servers, dict) or servers.get(MCP_SERVER_NAME) != desired:
        updated = _strip_owned_codex_table(updated, ("mcp_servers", MCP_SERVER_NAME))
        sep = "" if not updated or updated.endswith("\n") else "\n"
        updated += sep + codex_section()
    if not _codex_entry_is_current(updated, path=path):
        raise ValueError(f"Codex migration did not produce current settings: {path}")
    return updated


def upsert_codex(path: Path) -> bool:
    """Migrate owned MCP settings and default warning preference without redumping."""
    existing = _read_codex_config(path)
    updated = _prepare_codex_mcp(existing, path)
    if updated == existing:
        return False
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


@lru_cache(maxsize=1)
def _codex_editor():
    """Resolve the editor's TOML1.1 parsing and mutation capability."""
    try:
        import tomlkit

        probe = tomlkit.parse("probe={\n # retained\n enabled=false,\n}\n")
        probe["probe"]["enabled"] = True
        if probe.unwrap() != {"probe": {"enabled": True}} or "# retained" not in probe.as_string():
            raise ValueError("Codex editor capability probe returned an unexpected value")
    except Exception as exc:  # Installation failures only; user input is parsed below.
        raise RuntimeError(
            "Incompatible Booley installation: Codex config requires tomlkit>=0.15.2 with "
            "TOML1.1; reinstall dependencies or rebuild the compatible base"
        ) from exc
    return tomlkit


def _codex_document(path: Path):
    """Read and validate the permission document once, before registration effects."""
    _codex_toml_parser()  # Preserve the existing dependency diagnostic's precedence.
    editor = _codex_editor()
    existing = _read_codex_config(path)
    try:
        document = editor.parse(existing)
        _validate_codex_shape(document.unwrap())
    except (ValueError, editor.exceptions.TOMLKitError) as exc:
        raise ValueError(
            f"Invalid Codex config {path}: {exc}; repair this file and retry"
        ) from exc
    return existing, document


def _set_codex_root_keys(document, values: dict) -> None:
    """Update root keys in place; insert missing ones first, in order.

    Appending would land a new key after a trailing comment that introduces
    the next table, detaching that comment from its table.
    """
    position = 0
    prepend = bool(document.body)  # an empty document has nothing to insert before
    for key, value in values.items():
        if key in document or not prepend:
            document[key] = value
        else:
            document._insert_at(position, key, value)
            position += 1


def _prepare_codex_permissions(existing, document):
    """Edit owned permissions with the document library; preserve unrelated trivia."""
    editor = _codex_editor()
    desired = {
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        "web_search": "disabled",
    }
    if (
        all(document.get(key) == value for key, value in desired.items())
        and document.get("notice", {}).get("hide_full_access_warning") is True
    ):
        return existing
    _set_codex_root_keys(document, desired)
    if "notice" not in document:
        document["notice"] = editor.table()
    notice = document["notice"]
    key = "hide_full_access_warning"
    if key in notice:
        notice[key] = True
    else:
        value = editor.item(True)
        if isinstance(notice, editor.items.InlineTable):
            value.trivia.indent = " "
            value.trivia.trail = ""
            body = type(notice.value)(True)
            body.append(editor.key(key), value)
            if notice:
                body.append(None, editor.ws(","))
            for old_key, old_value in notice.value.body:
                body.append(old_key, old_value)
            document["notice"] = editor.items.InlineTable(body, notice.trivia)
        elif isinstance(notice, editor.container.OutOfOrderTableProxy):
            notice[key] = True
        elif notice.value.body:
            notice.value._insert_at(0, editor.key(key), value)
        else:
            notice[key] = value
    return document.as_string()


def _apply_codex_permission_mode(
    home: Path | None = None, *, prepared: tuple[str, str] | None = None
) -> str:
    """Publish standalone permissions or the registration preflight's prepared output."""
    path = codex_config_path(home)
    if prepared is None:
        existing, document = _codex_document(path)
        updated = _prepare_codex_permissions(existing, document)
    else:
        existing, updated = prepared
    if updated == existing:
        return "current"
    # Prepared from a validated document; registration's text also passed _codex_data.
    _publish_codex_config(path, updated, validated=True)
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
                    "Claude settings conflict with subscription policy; recreate the Sandbox"
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
                "Codex credential state conflicts with subscription policy; recreate the Sandbox"
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
    from booley.config.agent import env_auth_handoff
    from booley.config.gateway import GATEWAY_AUTH

    policy = env_auth_handoff() or "auto"
    if policy == GATEWAY_AUTH:  # already inside the container by definition
        # The gateway login is the provider written by _apply_codex_gateway;
        # there is no auth.json. Leave any Booley-managed credential copies
        # untouched and validate nothing.
        return "none"
    plan = auth_token.credential_plan(app, policy, home=home or _agent_home())
    credential = auth_token.credential_for_app(app)
    token = dict(plan.values).get(credential.env_var) if credential else None
    if policy != "auto":
        _validate_persisted_policy(app, policy, home)
    if app == auth_token.APP_CLAUDE:
        return _apply_claude_credential(token, home)
    if app == auth_token.APP_CODEX:
        return _apply_codex_credential(token, home)
    return "none"


# ---------------------------------------------------------------------------
# First-run state — no onboarding, login or folder-trust prompt in the Sandbox
# ---------------------------------------------------------------------------
#
# A Sandbox without a seeded host config (a Codespace, or any host with no
# agent config to seed from) would otherwise greet the first ``claude`` /
# ``codex`` with a theme picker, a login picker and a "trust this folder?"
# dialog. The Sandbox is already the isolation boundary (permission modes
# above), so trusting the Project's own directories adds no authority.
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
    from booley.config.agent import env_auth_handoff

    policy = env_auth_handoff() or "auto"
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
    """Trust *trust* paths, and with a credential skip onboarding and the theme picker.

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


def _with_codex_trust(text: str, key: str) -> str | None:
    """*text* with ``trust_level = "trusted"`` for *key*, or None when no layout fits.

    Tries the existing entry, a new ``[projects."key"]`` table, then a root
    dotted key. Each candidate must parse and carry the trust, so an inline
    ``projects = {...}`` or a dotted-defined entry never turns valid user TOML
    into invalid TOML.
    """
    parser = _codex_toml_parser()
    edited = _upsert_existing_codex_table_setting(
        text, table=("projects", key), key="trust_level", value='"trusted"'
    )
    spelled = json.dumps(key, ensure_ascii=False)
    separator = "" if not text or text.endswith("\n") else "\n"
    candidates = (
        [edited]
        if edited is not None
        else [
            f'{text}{separator}[projects.{spelled}]\ntrust_level = "trusted"\n',
            f'projects.{spelled}.trust_level = "trusted"\n{text}',
        ]
    )
    for candidate in candidates:
        try:
            trusted = parser.loads(candidate).get("projects", {}).get(key, {})
        except parser.TOMLDecodeError:
            continue
        if isinstance(trusted, dict) and trusted.get("trust_level") == "trusted":
            return candidate
    return None


def _apply_codex_trust(home: Path | None, trust: Sequence[str]) -> str:
    """Add ``trust_level = "trusted"`` for each *trust* path without a ``trust_level``.

    Edits the TOML in place, keeping comments and every other byte, and
    publishes once atomically. Returns ``"written"`` or ``"current"``.
    """
    path = codex_config_path(home)
    existing = _read_codex_config(path)
    projects = _codex_data(existing, path=path).get("projects", {})
    updated = existing
    for key in trust:
        if "trust_level" in projects.get(key, {}):
            continue
        edited = _with_codex_trust(updated, key)
        if edited is None:
            print(
                f"warning: Codex trust for {key} skipped: the projects layout in {path} "
                'cannot take a new entry; add trust_level = "trusted" there by hand',
                file=sys.stderr,
            )
            continue
        updated = edited
    if updated == existing:
        return "current"
    _publish_codex_config(path, updated)
    return "written"


# ---------------------------------------------------------------------------
# Gateway provider — interactive Codex under BOOLEY_PRIMARY_AUTH=gateway (#2270)
# ---------------------------------------------------------------------------

# Codex may wait for the token helper this much longer than the helper waits for
# the user's token command, so the helper reports the command's stderr first.
_AUTH_HELPER_MARGIN_S = 15


def _desired_provider(provider_toml: str) -> dict:
    """The parsed ``booley_gateway`` table a registration should leave in the config."""
    return _codex_toml_parser().loads(provider_toml)["model_providers"][GATEWAY_PROVIDER_ID]


def _gateway_exclude_list(existing: str, current: object) -> str | None:
    """Add the static token to ``shell_environment_policy.exclude``; ``None`` if unchanged."""
    from booley.config.gateway import GATEWAY_TOKEN_ENV

    if current is not None and not isinstance(current, list):
        return None  # the user's own non-list value wins; the caller reports the skip
    if current is not None and GATEWAY_TOKEN_ENV in current:
        return None
    names = [*(current or []), GATEWAY_TOKEN_ENV]
    value = json.dumps(names)
    table = ("shell_environment_policy",)
    updated = _upsert_existing_codex_table_setting(
        existing, table=table, key="exclude", value=value
    )
    if updated is not None:
        return updated
    return _upsert_codex_root_setting(existing, "shell_environment_policy.exclude", value)


def _with_gateway_settings(
    existing: str, data: dict, provider_toml: str, model_pin: str | None
) -> str:
    """Add the owned provider table and the set-if-absent root keys to *existing* TOML."""
    updated = existing
    providers = data.get("model_providers", {})
    if not isinstance(providers, dict) or providers.get(GATEWAY_PROVIDER_ID) != _desired_provider(
        provider_toml
    ):
        updated = _strip_owned_codex_table(updated, ("model_providers", GATEWAY_PROVIDER_ID))
        if updated and not updated.endswith("\n"):
            updated += "\n"
        updated += ("\n" if updated else "") + provider_toml
    if "model_provider" not in data:
        updated = _upsert_codex_root_setting(
            updated, "model_provider", json.dumps(GATEWAY_PROVIDER_ID)
        )
    if model_pin and "model" not in data:
        updated = _upsert_codex_root_setting(updated, "model", json.dumps(model_pin))
    features = data.get("features", {})
    if isinstance(features, dict) and "fast_mode" not in features:
        # The gateway rejects Codex's default priority tier (spike #2264).
        edited = _upsert_existing_codex_table_setting(
            updated, table=("features",), key="fast_mode", value="false"
        )
        updated = (
            edited
            if edited is not None
            else _upsert_codex_root_setting(updated, "features.fast_mode", "false")
        )
    policy = data.get("shell_environment_policy", {})
    excluded = _gateway_exclude_list(
        updated, policy.get("exclude") if isinstance(policy, dict) else None
    )
    return updated if excluded is None else excluded


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

    Only under ``BOOLEY_PRIMARY_AUTH=gateway``. Booley owns the provider table
    (a stale URL is replaced); ``model_provider``, ``model`` and
    ``features.fast_mode`` are written only when absent, so the user's keys
    win. Neither the token nor the token command reaches the file: ``auth``
    runs the helper, which runs the command on each Codex auth call.

    Returns ``off``, ``invalid`` (incomplete gateway environment), ``skipped``
    (a TOML layout that cannot take the entries), ``current`` or ``written``.
    """
    from booley.config import agent_app
    from booley.config.agent import env_auth_handoff
    from booley.config.gateway import GATEWAY_AUTH

    if env_auth_handoff() != GATEWAY_AUTH:
        return _drop_codex_gateway(home) if agent_app.auto_choice() else "off"
    path = codex_config_path(home)
    inputs = _gateway_inputs(path)
    if inputs is None:
        return "invalid"
    provider_toml, model_pin = inputs
    existing = _read_codex_config(path)
    data = _codex_data(existing, path=path)
    try:
        updated = _with_gateway_settings(existing, data, provider_toml, model_pin)
        data = _codex_data(updated, path=path)
    except ValueError:  # an edit left TOML the parser rejects: a layout we cannot extend
        data = None
    if data is None or not _gateway_entries_present(data, provider_toml):
        print(
            f"warning: Codex gateway provider skipped: the layout in {path} cannot take "
            "it; add the booley_gateway provider there by hand",
            file=sys.stderr,
        )
        return "skipped"
    _warn_gateway_overrides(data)
    if updated == existing:
        return "current"
    _publish_codex_config(path, updated)
    return "written"


def _strip_codex_root_key(existing: str, key: str) -> str:
    """Remove the root assignment of *key*, keeping every other byte."""
    return "".join(
        statement
        for statement, path, header in _toml_entries(existing)
        if header or path != (key,)
    )


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
    existing = _read_codex_config(path)
    data = _codex_data(existing, path=path)
    updated = _strip_owned_codex_table(existing, ("model_providers", GATEWAY_PROVIDER_ID))
    if data.get("model_provider") == GATEWAY_PROVIDER_ID:
        updated = _strip_codex_root_key(updated, "model_provider")
        try:
            pin = gateway_model_pin_from_env().model
        except GatewayConfigError:
            pin = None
        if pin and data.get("model") == pin:
            updated = _strip_codex_root_key(updated, "model")
    if updated == existing:
        return "off"
    _publish_codex_config(path, updated)
    return "removed"


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
        existing, document = _codex_document(path)
        permissions = _prepare_codex_permissions(existing, document)
        mcp_only = _prepare_codex_mcp(existing, path)
        combined = _prepare_codex_mcp(permissions, path)
        changed = mcp_only != existing
        if changed:
            _publish_codex_config(path, mcp_only, validated=True)
        linked = deploy_skills(app, home)
        host_linked = deploy_host_skills(app, home)
        cred = apply_stored_credential(app, home)
        perm = _apply_codex_permission_mode(home, prepared=(mcp_only, combined))
        first = _apply_codex_trust(home, trust_paths)
        gateway = _apply_codex_gateway(home)
        mcp = "written" if changed else "current"
        return (
            f"codex:{mcp} skills:+{linked} host-skills:+{host_linked} "
            f"cred:{cred} perm:{perm} first-run:{first} gateway:{gateway}"
        )
    return "none"
