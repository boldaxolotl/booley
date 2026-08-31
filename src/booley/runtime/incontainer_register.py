"""Register and start the Booley MCP server *inside* the container.

ADR 0018/0023: the MCP server lives inside the dev container and serves
Interactive Mode over **streamable HTTP on loopback**. This module, run from
the devcontainer ``postCreateCommand``/``postStartCommand`` as ``python -m
booley.runtime.incontainer_register``, does two things on every container start:

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
import time
import tomllib
from pathlib import Path

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
    try:
        with Path(log_path).open("ab") as log:
            proc = subprocess.Popen(
                _SERVER_CMD,
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
# Per-app skills dir mirrors the host model (init Step 8 / deploy_skills.py):
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


def _codex_entry_is_current(existing: str) -> bool:
    """Whether *existing* already carries exactly the desired booley entry."""
    try:
        parsed = tomllib.loads(existing)
    except tomllib.TOMLDecodeError:
        return False
    entry = parsed.get("mcp_servers", {}).get(MCP_SERVER_NAME)
    return entry == {"url": http_url(), "tool_timeout_sec": _TOOL_TIMEOUT_SEC}


def _strip_codex_table(existing: str) -> str:
    """Drop the ``[mcp_servers.booley]`` table (and subtables) from *existing*.

    Line-based on purpose: rewriting only Booley's own table keeps every user
    comment and unrelated section byte-identical, which a parse→re-dump of the
    whole file would not.
    """
    header = f"[mcp_servers.{MCP_SERVER_NAME}]"
    subheader_prefix = f"[mcp_servers.{MCP_SERVER_NAME}."
    out: list[str] = []
    skipping = False
    for line in existing.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped == header or stripped.startswith(subheader_prefix)
        if not skipping:
            out.append(line)
    return "".join(out)


def upsert_codex(path: Path) -> bool:
    """Ensure the Booley MCP table matches the desired form. Returns True if changed.

    An up-to-date entry is left untouched; a stale one (e.g. the pre-ADR-0023
    stdio ``command``/``args`` form, which Codex would otherwise keep spawning
    as a doomed per-session child) is replaced in place. User comments and
    other sections are preserved.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if f"[mcp_servers.{MCP_SERVER_NAME}]" in existing:
        if _codex_entry_is_current(existing):
            return False
        existing = _strip_codex_table(existing)
    sep = (
        ""
        if not existing or existing.endswith("\n\n")
        else ("\n" if existing.endswith("\n") else "\n\n")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + sep + codex_section(), encoding="utf-8")
    return True


def _upsert_codex_root_setting(existing: str, key: str, value: str) -> str:
    """Set one root scalar without disturbing comments or unrelated tables."""
    lines = existing.splitlines(keepends=True)
    root_end = next(
        (index for index, line in enumerate(lines) if line.strip().startswith("[")),
        len(lines),
    )
    replacement = f"{key} = {value}\n"
    for index in range(root_end):
        if lines[index].partition("=")[0].strip() == key:
            lines[index] = replacement
            return "".join(lines)
    lines.insert(root_end, replacement)
    return "".join(lines)


def _upsert_codex_full_access_notice(existing: str) -> str:
    """Acknowledge Codex's one-time full-access warning in existing TOML."""
    lines = existing.splitlines(keepends=True)
    header_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "[notice]"),
        None,
    )
    if header_index is None:
        return _upsert_codex_root_setting(existing, "notice.hide_full_access_warning", "true")

    section_end = next(
        (
            index
            for index in range(header_index + 1, len(lines))
            if lines[index].strip().startswith("[")
        ),
        len(lines),
    )
    for index in range(header_index + 1, section_end):
        if lines[index].partition("=")[0].strip() == "hide_full_access_warning":
            lines[index] = "hide_full_access_warning = true\n"
            return "".join(lines)
    lines.insert(header_index + 1, "hide_full_access_warning = true\n")
    return "".join(lines)


def _apply_codex_permission_mode(home: Path | None = None) -> str:
    """Pin Codex to its container-trusted, provider-web-disabled mode."""
    path = codex_config_path(home)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        data = tomllib.loads(existing)
    except tomllib.TOMLDecodeError:
        data = {}
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
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


def launch_auto_doctor(project_root: Path | None = None) -> str:
    """Start the stale-triggered health worker; registration never depends on it."""
    try:
        from booley.harness.auto_doctor import launch

        return launch(project_root or Path.cwd())
    except Exception:  # noqa: BLE001 — postStart health is advisory; MCP registration must survive
        return "failed"


def observe_upgrade(project_root: Path | None = None) -> str:
    """Observe the in-container package version without blocking registration."""
    try:
        from booley.harness import upgrade_cli, upgrade_review
        from booley.runtime.project_dir import resolve_project_dir

        status = upgrade_review.observe(resolve_project_dir(project_root or Path.cwd()))
        if status.condition is not upgrade_review.ReviewCondition.CURRENT:
            print(f"warning: {upgrade_cli.render_status(status)}", file=sys.stderr)
        return status.condition.value
    except Exception:  # noqa: BLE001 — postStart upgrade advice is fail-soft
        return "unavailable"


def main() -> None:
    app = os.environ.get("BOOLEY_AGENT_APP", "none")
    # Server first, registration second: the entry should point at a live URL
    # by the time the agent app reads its config at session start. With no
    # agent app there is no client, so nothing to serve or register.
    server = "skipped" if app == "none" else ensure_http_server()
    status = register(app)
    upgrade = observe_upgrade()
    health = launch_auto_doctor()
    print(
        f"booley incontainer-register: server:{server} upgrade:{upgrade} health:{health} {status}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
