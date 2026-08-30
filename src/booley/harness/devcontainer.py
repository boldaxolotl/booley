"""Generate the untracked ``.devcontainer/devcontainer.json`` for Interactive Mode.

ADR 0018: Interactive Mode is entered by opening the repo folder in VS Code and
accepting "Reopen in Container". The runtime is described by a single,
Booley-generated ``.devcontainer/devcontainer.json`` that lives in the repo but
is never tracked by git (hidden via ``.git/info/exclude``, not ``.gitignore``).

This module is the pure spec builder plus a thin writer. It owns no Docker or
git side effects beyond writing the JSON file; network/proxy/reaper creation
(WS1/WS2) and the exclude entry (via :func:`harness.git_utils.add_git_excludes`)
live in the ``booley init`` flow.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from booley.config.agent import SANDBOX_IMAGE
from booley.runtime import auth_token
from booley.runtime.timefmt import LOCAL_TIMEZONE_ENV

# --- Supported agent apps (mirrors the init wizard's app selection) ---
APP_CLAUDE = "claude"
APP_CODEX = "codex"
APP_NONE = "none"
SUPPORTED_APPS = (APP_CLAUDE, APP_CODEX, APP_NONE)

# --- Long-lived Docker objects created by ``booley init`` (WS1/WS2) ---
# The session container attaches to this --internal network; the dual-homed
# ``booley-proxy`` is its sole egress path.
# Versioned because the original ``booley-egress`` used Docker's default
# internal-bridge gateway, which remained reachable from Session containers.
# A distinct name lets ``booley init`` migrate without disrupting an already
# running legacy Session; new specs can only attach to the host-isolated v2.
EGRESS_NETWORK = "booley-egress-v2"
PROXY_HOST = "booley-proxy"
PROXY_PORT = 8080
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"

# Label the idle reaper (WS2) matches to find/own session containers.
INTERACTIVE_ROLE_LABEL = "booley.role=interactive"

# Process cap for the whole Session Runtime (fork-bomb ceiling, not a tuning
# knob). Sized far above the retired per-Flow sandbox's 512 because this one
# container hosts the VS Code server, the agent CLI, the in-container
# developer, and up to ``[jobs] max_tickets`` concurrent tickets each
# spawning EDA tools (ADR 0028). Its only job is to bound a hijacked agent's
# fork bomb; legitimate peak process counts stay well under this.
SESSION_PIDS_LIMIT = 4096

# --- Container-side paths (must match the booley-sandbox image / developer) ---
WORK_DIR = "/work"
# ``.booley_project`` mounts here; BOOLEY_PROJECT_DIR (containerEnv, below)
# points at it so in-container tooling — including the Ticket-Mode Runner and
# its developer agent (ADR 0028) — resolves project config from one place.
PROJECT_DIR_TARGET = "/booley-project"
AGENT_HOME = "/home/agent"  # non-root ``agent`` user (see data/docker/Dockerfile)

# Workspace-relative basename of the project dir. ``[sandbox] mask_paths``
# needs it to notice entries that are visible through TWO container mounts:
# the whole workspace binds at /work (so ``.booley_project/x`` shows up at
# ``/work/.booley_project/x``) AND the project dir itself binds again at
# PROJECT_DIR_TARGET — masking only the /work view would leave the same bytes
# readable at ``/booley-project/x``.
PROJECT_DIR_BASENAME = ".booley_project"

# Default mount source for ``.booley_project``: the co-located common case.
# The Dev Containers CLI does NOT support nested variable defaults
# (``${localEnv:VAR:${localWorkspaceFolder}/...}`` is mis-parsed into an invalid
# mount path), so for non-co-located projects ``booley init`` bakes the resolved
# absolute host path instead of relying on env-var indirection here.
DEFAULT_PROJECT_DIR_SOURCE = "${localWorkspaceFolder}/.booley_project"

# VS Code Marketplace extension IDs for the supported agent apps. Listed under
# ``customizations`` so VS Code installs them container-side; update if the
# published identifiers change.
_APP_EXTENSION = {
    # Canonical Marketplace IDs (publisher.name); verified against the
    # VS Code Marketplace / official docs (2026-06).
    APP_CLAUDE: "Anthropic.claude-code",
    APP_CODEX: "openai.chatgpt",
}

# Waveform Viewer (ADR 0035): VaporView is installed for every app so
# `bwave gui` and the agent's scoped-view WCP calls work in the attached
# window.
_VAPORVIEW_EXTENSION = "lramseyer.vaporview"

# Verilog/SystemVerilog syntax highlighting: without an HDL grammar the
# attached window renders every .v/.sv file as plain text. mshr-h's extension
# ships the TextMate grammar (plus ctags-based navigation) and keeps linting
# OFF by default, so it adds highlighting without competing with Booley's own
# lint flow. Installed for every app — reading RTL isn't tied to which agent
# is attached.
_HDL_EXTENSION = "mshr-h.veriloghdl"

# Tcl syntax highlighting: constraint files (SDC/XDC, ADR 0029/0031) are Tcl,
# and synth/impl flows are driven by Tcl scripts. The grammar alone doesn't
# claim the .sdc/.xdc suffixes, so _HIGHLIGHT_FILE_ASSOCIATIONS maps them to
# the tcl language id.
_TCL_EXTENSION = "bitwisecook.tcl"

# Microsoft's Live Preview hosts static HTML and renders it in an embedded
# VS Code tab. Installed for every app so human-facing reports (including the
# review HTML under the separately mounted project dir) are viewable
# without a browser in the sandbox.
_LIVE_PREVIEW_EXTENSION = "ms-vscode.live-server"

# Live Preview exposes its in-container HTTP/WebSocket servers through VS
# Code's remote-port tunnel. VS Code's long-lived local process can retain a
# dead tunnel after its original container exits, then hand that tunnel to a
# later container asking for the same default 3000/3001 pair (F-14). Seed a
# safe high port, disable restoration, and replace the seed with a fresh pair
# from ``postAttachCommand`` before the extension activates.
_LIVE_PREVIEW_SEED_PORT = 61000
_LIVE_PREVIEW_SETTINGS = {
    "remote.restoreForwardedPorts": False,
    "livePreview.portNumber": _LIVE_PREVIEW_SEED_PORT,
}

# files.associations entries that route constraint suffixes to the Tcl grammar
# above; emitted into the spec's VS Code settings for every app.
_HIGHLIGHT_FILE_ASSOCIATIONS = {"*.sdc": "tcl", "*.xdc": "tcl"}

# The Session Runtime installs the framework and its Python dependencies system-wide;
# a workspace ``.venv`` belongs to the host and is neither needed nor reliably
# usable in the container.  VS Code's Python Environments extension otherwise
# discovers it asynchronously and sends ``source .venv/bin/activate`` to every
# new terminal.  Besides arriving several seconds after the prompt, that input
# can land while an interactive CLI/TUI owns the terminal.  Set both the current
# machine-scoped control and its legacy predecessor: the former takes
# precedence in recent releases, while the latter covers older Python installs.
_PYTHON_TERMINAL_SETTINGS = {
    "python-envs.terminal.autoActivationType": "off",
    "python.terminal.activateEnvironment": False,
}

# VaporView's WCP control port, pinned because bwave's WCP client discovers
# the server at a fixed port (bwave/wcp.py WCP_DEFAULT_PORT must agree;
# 0 = auto-assign would break discovery).
_VAPORVIEW_WCP_PORT = 54322

# Where each app reads its auth token inside the container (agent user's home).
_APP_AUTH_TARGET = {
    APP_CLAUDE: f"{AGENT_HOME}/.claude/.credentials.json",
    APP_CODEX: f"{AGENT_HOME}/.codex/auth.json",
}

# Per-app home-state dir to persist across container rebuilds. The container's
# writable layer is discarded on rebuild, so without this the agent loses its
# plans, session transcripts, and todos (e.g. ~/.claude/plans/*.md). Backed by a
# named Docker volume (see :func:`_state_volume_mount`).
_APP_STATE_DIR = {
    APP_CLAUDE: f"{AGENT_HOME}/.claude",
    APP_CODEX: f"{AGENT_HOME}/.codex",
}

# Claude Code's user config (``~/.claude.json``) caches statsig feature-gate
# evaluations — including staged-rollout / promotional model grants (e.g.
# Fable) — plus the install stableID and onboarding state. It lives at
# ``$HOME/.claude.json``, a *sibling* of the persisted ``~/.claude/`` state dir
# (see ``_APP_STATE_DIR``), so the state volume does NOT cover it and it is
# reborn empty on every container (re)create. A fresh config re-rolls the
# stableID and drops the cached grants, so gated models silently vanish from
# the in-container ``/model`` picker even when the CLI, extension, and
# credentials all match the host. We seed the container's copy from the host's
# config (read-only bind + copy) so the grants carry over;
# ``incontainer_register`` then merges the container-local ``mcpServers.booley``
# entry on top (``upsert_claude`` preserves existing keys). Only Claude keeps
# this cache — Codex has no equivalent.
_CLAUDE_CONFIG_TARGET = f"{AGENT_HOME}/.claude.json"
_CLAUDE_CONFIG_SEED_TARGET = f"{AGENT_HOME}/.claude-config-seed.json"

# Both apps keep a REFRESHING credential in the file we mount, and both REWRITE
# that file when the token refreshes:
#   - Claude's ``~/.claude/.credentials.json`` holds the OAuth access + refresh
#     tokens and is rewritten on every refresh.
#   - Codex's ``~/.codex/auth.json`` holds a static API key only when signed in
#     with one; a ChatGPT-subscription login stores refreshing OAuth tokens in
#     the same file, and Codex rewrites it too.
# Binding the host file read-only directly onto that path makes the in-container
# refresh write FAIL — and in a headless ticket container nothing on the host
# refreshes on its behalf — so the session 401s the moment the mounted token
# expires. Instead we bind the host credential read-only at a sidecar *seed* path
# (outside the app's state dir, to dodge state-volume nesting) and copy it onto
# the real credentials file, which lives on the writable state volume. The
# in-container refresh can then rewrite the copy freely and the host file is
# never touched.
#
# The copy runs on postStart as well as postCreate: a credential is the one piece
# of seeded state that goes STALE while a container merely sits stopped. A
# container created days ago and resumed with `docker start` used to keep its
# long-dead create-time token and fail every agent session with "Not logged in".
_APP_CREDS_SEED_TARGET = {
    APP_CLAUDE: f"{AGENT_HOME}/.claude-creds-seed.json",
    APP_CODEX: f"{AGENT_HOME}/.codex-auth-seed.json",
}

# Sidecar for the ROTATION-FREE credential stored by ``booley auth`` (Claude's
# one-year setup-token, Codex's API key). It rides in as its own read-only bind
# because the remoteEnv ``${localEnv:...}`` route cannot deliver it to VS
# Code's "Reopen in Container": VS Code resolves localEnv against its own
# process env, where the stored file is invisible. ``incontainer_register``
# reads this sidecar on every container start and applies it container-side
# (see :func:`booley.runtime.incontainer_register.apply_stored_credential`), so every
# entry point — VS Code, ``booley session``, headless drivers — sees the same
# credential with no manual export. Same home-sidecar placement rationale as
# ``_APP_CREDS_SEED_TARGET`` above.
_APP_TOKEN_SEED_TARGET = {
    app: f"{AGENT_HOME}/{name}" for app, name in auth_token.TOKEN_SEED_BASENAME.items()
}

# Sidecar root for the user's HOST agent skills ([sandbox] mount_host_skills).
# Each host skill dir rides in as its own read-only bind at
# ``<sidecar>/<name>``; ``incontainer_register`` then reconciles each into the
# app's real skills dir (``~/.claude/skills`` / ``~/.agents/skills``) on every
# container start, with built-ins taking explicit name precedence. Kept OUTSIDE
# the app state dir (same
# state-volume-nesting rationale as the credential sidecars above), and the
# read-only bind makes the whole tree unwritable, so a mounted host skill can
# never be mutated from inside the sandbox.
HOST_SKILLS_SIDECAR = f"{AGENT_HOME}/.booley-host-skills"


def mcp_post_start_command() -> str:
    """Shell command for ``postStartCommand``: start + register the in-container MCP.

    The registrar starts the loopback streamable-HTTP MCP server (unless one is
    already serving) and writes the URL registration to the agent's
    container-side config (ADR 0023). Because the hook re-runs on every
    container start — including a plain stop→start resume — the endpoint is
    always back before the agent app needs it; the app holds only the URL, so
    it reconnects instead of being stranded with a dead stdio child. The app
    to register comes from ``BOOLEY_AGENT_APP``.
    """
    return "python -m booley.runtime.incontainer_register"


def vaporview_patch_command() -> str:
    """Shell command for ``postAttachCommand``: make VaporView's WCP server
    auto-start on window load.

    Runs on *attach* (not create/start) because it must edit the VaporView
    extension's manifest, which VS Code installs container-side only once an extension
    attaches — by postCreate/postStart it isn't on disk yet. The patcher relaxes
    the ``vaporview.wcp.enabled``/``.port`` setting scope so the spec's
    Machine-settings values are honored, and makes the extension activate
    eagerly so its own auto-start runs without an open waveform tab; without
    both, ``wcp.enabled: true`` is inert and the server never binds until a human
    runs the palette command. Idempotent and never fails the hook. See
    :mod:`booley.runtime.incontainer_vaporview` for the full rationale.
    """
    return "python -m booley.runtime.incontainer_vaporview"


def live_preview_port_command() -> str:
    """Shell command that assigns Live Preview fresh remote ports on attach."""
    return "python -m booley.runtime.incontainer_live_preview"


def post_attach_command() -> str:
    """Run container UI compatibility setup before attached extensions activate."""
    return f"{live_preview_port_command()} && {vaporview_patch_command()}"


def _post_attach_has(spec: dict, command: str) -> bool:
    """Whether *command* is one complete step in the generated attach hook."""
    value = spec.get("postAttachCommand")
    return isinstance(value, str) and command in (part.strip() for part in value.split("&&"))


def _vscode_extension_for_app(app: str) -> str | None:
    """Return the VS Code extension ID for *app*, or ``None`` for ``"none"``."""
    return _APP_EXTENSION.get(app)


def _auth_target_for_app(app: str) -> str | None:
    """Return the container-side auth-token path for *app*, or ``None``."""
    return _APP_AUTH_TARGET.get(app)


def _creds_seed_target_for_app(app: str) -> str | None:
    """Return the sidecar seed path *app*'s host credential is bound at."""
    return _APP_CREDS_SEED_TARGET.get(app)


def _bind_mount(source: str, target: str, *, readonly: bool = False) -> str:
    """Render a devcontainer ``mounts`` bind-mount string."""
    parts = [f"source={source}", f"target={target}", "type=bind"]
    if readonly:
        parts.append("readonly")
    return ",".join(parts)


def _volume_mount(source: str, target: str) -> str:
    """Render a devcontainer ``mounts`` named-volume string."""
    return f"source={source},target={target},type=volume"


def canonical_project_id(project_root: Path) -> str:
    """Stable collision-resistant identity for one canonical Project root."""
    canonical = os.path.normcase(str(project_root.resolve(strict=True))).encode()
    return hashlib.sha256(canonical).hexdigest()[:32]


def state_volume_name(app: str, project_id: str) -> str | None:
    """Name of the persistent home-state volume for *app* and *project_id*.

    Returns ``None`` for apps without a persisted state dir. The single format
    source so the spec builder and ``booley doctor`` agree on names.
    """
    if app not in _APP_STATE_DIR:
        return None
    return f"booley-{app}-state-{project_id}"


def state_volume_mount(app: str, project_id: str) -> str | None:
    """Return the persistent home-state volume mount for *app*, or ``None``.

    Per-project + per-app naming keeps projects isolated: every interactive
    container mounts the workspace at the same ``/work``, so a shared volume
    would collide their session transcripts.
    """
    target = _APP_STATE_DIR.get(app)
    if target is None:
        return None
    name = state_volume_name(app, project_id)
    return _volume_mount(name, target)


def _parse_mount(mount: object) -> dict[str, str]:
    """Parse one devcontainer ``mounts`` entry into a ``{key: value}`` dict.

    Booley renders mounts as ``"source=..,target=..,type=.."`` strings (see
    :func:`_bind_mount` / :func:`_volume_mount`); the Dev Containers schema also
    permits the object form, which we accept defensively. Anything else -> ``{}``.
    """
    if isinstance(mount, dict):
        return {str(k): str(v) for k, v in mount.items()}
    if isinstance(mount, str):
        return dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
    return {}


def spec_mounts_target(spec: dict, target: str) -> bool:
    """Whether a devcontainer *spec* mounts any source at *target*."""
    mounts = spec.get("mounts")
    return any(
        _parse_mount(mount).get("target") == target
        for mount in (mounts if isinstance(mounts, list) else [])
    )


def spec_agent_app(spec: dict) -> str | None:
    """The agent app *spec* configures, from ``remoteEnv.BOOLEY_AGENT_APP``.

    :func:`build_devcontainer_spec` always exports this, and it is what the
    in-container registrar reads to decide which app's config gets the Booley
    MCP entry — so every "which app is this spec for?" question below routes
    through here. ``None`` when absent or malformed.
    """
    remote_env = spec.get("remoteEnv")
    app = remote_env.get("BOOLEY_AGENT_APP") if isinstance(remote_env, dict) else None
    return app if isinstance(app, str) else None


def spec_state_is_persisted(spec: dict) -> bool | None:
    """Whether *spec* persists its agent app's home-state across rebuilds.

    A spec generated before the persistence fix mounts no volume at the agent's
    home-state dir, so the container's writable layer — and with it every session
    transcript, plan, and todo — is silently discarded on each rebuild. This is
    the single detector both the spec builder's contract and ``booley doctor``
    read, so a stale on-disk spec can be surfaced instead of passing unnoticed.

    Returns:
        ``True``  - the app keeps home-state and a ``type=volume`` mount targets
                    its state dir (e.g. ``/home/agent/.claude``): survives rebuilds.
        ``False`` - the app keeps home-state but NO volume mount targets it: a
                    STALE spec predating the fix; in-container history is lost.
        ``None``  - the app has no persistent state dir (nothing to persist), or
                    the app is absent/unknown; there is nothing to check.

    The app is read from ``remoteEnv.BOOLEY_AGENT_APP`` (always set by
    :func:`build_devcontainer_spec`).
    """
    target = _APP_STATE_DIR.get(spec_agent_app(spec))
    if target is None:
        return None
    mounts = spec.get("mounts")
    for mount in mounts if isinstance(mounts, list) else []:
        parts = _parse_mount(mount)
        if parts.get("type") == "volume" and parts.get("target") == target:
            return True
    return False


def spec_mounts_token_seed(spec: dict) -> bool | None:
    """Whether *spec* mounts the app's ``booley auth`` token-seed sidecar.

    A spec seeded before a credential was stored (or before this mount existed)
    carries no sidecar, so VS Code sessions silently fall back to the refreshing
    subscription credential — the exact failure mode ``booley auth`` exists to
    prevent. ``booley doctor`` reads this to surface the drift.

    Returns ``None`` when the spec's app has no rotation-free credential
    (nothing to mount), else whether the sidecar mount is present. The app is
    read from ``remoteEnv.BOOLEY_AGENT_APP``, same as
    :func:`spec_state_is_persisted`.
    """
    target = _APP_TOKEN_SEED_TARGET.get(spec_agent_app(spec))
    if target is None:
        return None
    mounts = spec.get("mounts")
    for mount in mounts if isinstance(mounts, list) else []:
        if _parse_mount(mount).get("target") == target:
            return True
    return False


def spec_installs_vaporview(spec: dict) -> bool:
    """Whether *spec* installs VaporView and wires its WCP control server.

    The Waveform Viewer (ADR 0035) reaches an attached VS Code window only
    through the generated spec: the ``lramseyer.vaporview`` extension install,
    the ``vaporview.wcp.enabled``/``vaporview.wcp.port`` settings, AND the
    ``postAttachCommand`` manifest patch that actually makes the WCP server
    auto-start (the setting alone is inert — it is application-scoped, so the
    container's Machine settings are ignored, and the extension activates
    lazily). A spec seeded before any of these landed leaves every scoped
    ``bwave gui`` failing with "WCP server not running" — even after an image
    rebuild, which never touches the spec. ``booley doctor`` reads this to
    surface the drift.

    Returns ``True`` only when the extension is listed, the WCP server is
    enabled on the port bwave's client expects, AND the auto-start patch is
    wired; any miss means a stale or hand-edited spec that a re-seed would fix.
    """
    customizations = spec.get("customizations")
    vscode = customizations.get("vscode") if isinstance(customizations, dict) else None
    if not isinstance(vscode, dict):
        return False
    extensions = vscode.get("extensions")
    if not (isinstance(extensions, list) and _VAPORVIEW_EXTENSION in extensions):
        return False
    settings = vscode.get("settings")
    if not isinstance(settings, dict):
        return False
    if not (
        settings.get("vaporview.wcp.enabled") is True
        and settings.get("vaporview.wcp.port") == _VAPORVIEW_WCP_PORT
    ):
        return False
    # The auto-start patch runs from postAttachCommand; without it the settings
    # above never take effect (see vaporview_patch_command).
    return _post_attach_has(spec, vaporview_patch_command())


def spec_installs_hdl_highlight(spec: dict) -> bool:
    """Whether *spec* installs the RTL + constraints highlighting extensions.

    Like VaporView, the grammars reach an attached window only through the
    generated spec's ``customizations`` — never via the sandbox image — so a
    spec seeded before they landed renders RTL and SDC/XDC constraints as
    plain text until a re-seed. Requires the Verilog/SystemVerilog and Tcl
    extensions AND the ``files.associations`` entries that route the
    constraint suffixes to the Tcl grammar (the extension alone doesn't claim
    them). ``booley doctor`` reads this to surface the drift.
    """
    customizations = spec.get("customizations")
    vscode = customizations.get("vscode") if isinstance(customizations, dict) else None
    if not isinstance(vscode, dict):
        return False
    extensions = vscode.get("extensions")
    if not (
        isinstance(extensions, list)
        and _HDL_EXTENSION in extensions
        and _TCL_EXTENSION in extensions
    ):
        return False
    settings = vscode.get("settings")
    associations = settings.get("files.associations") if isinstance(settings, dict) else None
    if not isinstance(associations, dict):
        return False
    return all(
        associations.get(pattern) == lang for pattern, lang in _HIGHLIGHT_FILE_ASSOCIATIONS.items()
    )


def spec_installs_live_preview(spec: dict) -> bool:
    """Whether *spec* installs and safely configures the HTML report viewer.

    Live Preview is delivered by VS Code from the devcontainer spec, not by the
    runtime image. Restored remote-port tunnels can retain Live Preview's
    default ports without a live server, producing a blank embedded preview.
    The health check uses this detector to identify projects whose spec either
    predates rendered HTML reports or still restores stale forwarded ports.
    """
    customizations = spec.get("customizations")
    vscode = customizations.get("vscode") if isinstance(customizations, dict) else None
    if not isinstance(vscode, dict):
        return False
    extensions = vscode.get("extensions")
    settings = vscode.get("settings")
    return (
        isinstance(extensions, list)
        and _LIVE_PREVIEW_EXTENSION in extensions
        and isinstance(settings, dict)
        and all(settings.get(key) == value for key, value in _LIVE_PREVIEW_SETTINGS.items())
        and _post_attach_has(spec, live_preview_port_command())
    )


def spec_disables_python_terminal_activation(spec: dict) -> bool:
    """Whether VS Code is told not to activate workspace virtualenvs.

    Python extension installs can follow users into a devcontainer through
    Settings Sync.  The current Python Environments extension and older Python
    extension use different controls, so the rendered spec carries both.
    Doctor reads this detector to prompt existing projects to
    regenerate their untracked spec and receive the terminal behavior fix.
    """
    customizations = spec.get("customizations")
    vscode = customizations.get("vscode") if isinstance(customizations, dict) else None
    settings = vscode.get("settings") if isinstance(vscode, dict) else None
    if not isinstance(settings, dict):
        return False
    return (
        settings.get("python-envs.terminal.autoActivationType") == "off"
        and settings.get("python.terminal.activateEnvironment") is False
    )


def mask_paths_error(value: object) -> str | None:
    """Validation error for a ``[sandbox].mask_paths`` value, or ``None``.

    The knob is a list of workspace-root-relative POSIX paths to HIDE from the
    Session Runtime (each becomes a read-only bind of an always-empty host dir
    over ``/work/<rel>``, see :func:`_mask_mounts`). Only clean relative
    subpaths are accepted: an absolute path would mask an arbitrary container
    path and a ``..`` segment would walk the mask outside the workspace mount —
    either turns a privacy knob into a container-layout editor. Masking
    ``.booley_project`` itself is rejected too: the session reads its config,
    tickets, and worktrees through that mount (``BOOLEY_PROJECT_DIR``), so
    hiding the whole dir bricks the runtime — mask *subtrees* of it instead.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return "[sandbox].mask_paths must be a list of strings"
    bad: list[str] = []
    whole_project_dir: list[str] = []
    for v in value:
        # PurePosixPath normalizes away "." segments, duplicate and trailing
        # slashes, so ``parts`` is the canonical segment view ("." -> ()).
        parts = PurePosixPath(v).parts
        if (
            not parts
            or PurePosixPath(v).is_absolute()
            or "\\" in v  # the knob is POSIX-only; a Windows path is a mistake
            or ".." in parts
        ):
            bad.append(v)
        elif parts == (PROJECT_DIR_BASENAME,):
            whole_project_dir.append(v)
    if bad:
        return (
            "[sandbox].mask_paths entries must be workspace-relative POSIX "
            "paths with no '..' segments: " + ", ".join(repr(v) for v in bad)
        )
    if whole_project_dir:
        return (
            f"[sandbox].mask_paths must not mask {PROJECT_DIR_BASENAME!r} "
            "itself — the session reads its config through that mount; mask "
            f"subtrees (e.g. {PROJECT_DIR_BASENAME}/private) instead"
        )
    return None


def _mask_mounts(mask_paths: Sequence[str], mask_source: str) -> list[str]:
    """Read-only empty binds hiding ``[sandbox] mask_paths`` subtrees.

    Each entry becomes a bind of *mask_source* — a dedicated ALWAYS-EMPTY host
    dir — over ``/work/<rel>``, so the subtree simply reads as an empty dir in
    the Session Runtime. Since ADR 0028 every agent is an in-container
    subprocess sharing the one workspace mount (no per-agent mounts), an
    over-mount is the only per-path visibility control left. Two deliberate
    choices:

    - A bind of an empty dir, NOT a tmpfs mount: the Dev Containers CLI mount
      schema does not reliably support tmpfs entries, and an empty read-only
      bind behaves identically for hiding purposes.
    - ``readonly``: the mask must also swallow writes — an agent writing into
      a masked subtree would otherwise scribble into the shared empty dir.

    An entry under ``.booley_project/`` is masked a SECOND time under
    ``/booley-project/<rest>``: the project dir is bind-mounted twice (the
    workspace mount at /work plus its own mount at PROJECT_DIR_TARGET), and
    masking only one view leaves the same bytes readable through the other.

    Callers must place these AFTER the project-dir mount in the ``mounts``
    list: the Dev Containers CLI and :func:`session_runtime.docker_run_argv`
    both emit workspaceMount first, then ``mounts`` in order, so a later mask
    over-mounts its parent instead of being buried by it.
    """
    mounts: list[str] = []
    for rel in mask_paths:
        # Canonical segments (mask_paths_error already vetted them): trailing
        # slashes / "." segments normalize away so the mount target is clean.
        parts = PurePosixPath(rel).parts
        if not parts:
            continue  # belt-and-braces: validation already rejects "" / "."
        mounts.append(_bind_mount(mask_source, f"{WORK_DIR}/{'/'.join(parts)}", readonly=True))
        if parts[0] == PROJECT_DIR_BASENAME and len(parts) > 1:
            rest = "/".join(parts[1:])
            mounts.append(_bind_mount(mask_source, f"{PROJECT_DIR_TARGET}/{rest}", readonly=True))
    return mounts


def _build_mounts(
    app: str,
    project_id: str,
    project_dir_source: str,
    auth_token_source: str | None,
    auth_target: str | None,
    seeding_creds: bool,
    config_seed_source: str | None,
    seeding_config: bool,
    token_seed_source: str | None,
    host_skills: Sequence[tuple[str, str]],
    trusted_eda_mounts: Sequence[tuple[str, str]],
    protected_devcontainer_source: str,
    mask_paths: Sequence[str] = (),
    mask_source: str = "",
) -> list[str]:
    """Build the ``mounts`` list for :func:`build_devcontainer_spec`."""
    mounts = [_bind_mount(project_dir_source, PROJECT_DIR_TARGET)]
    # Rotation-free credential stored by `booley auth` (see
    # _APP_TOKEN_SEED_TARGET): mounted read-only for incontainer_register to
    # apply on every start. Re-minting rewrites the host file in place (same
    # inode), so a running container sees the fresh value on its next start;
    # only `booley auth --clear` (an unlink) needs a container rebuild.
    token_seed_target = _APP_TOKEN_SEED_TARGET.get(app)
    if token_seed_source and token_seed_target:
        mounts.append(_bind_mount(token_seed_source, token_seed_target, readonly=True))
    # The app's credential file is refreshed in-container (see
    # _APP_CREDS_SEED_TARGET): bind the host credential read-only at the sidecar
    # seed path and copy it onto the writable real path, so the in-container
    # refresh can write. A direct read-only bind at the auth target would make
    # that write fail and 401 the session once the token expires.
    if auth_token_source and auth_target:
        creds_bind_target = _creds_seed_target_for_app(app) if seeding_creds else auth_target
        mounts.append(_bind_mount(auth_token_source, creds_bind_target, readonly=True))
    # Seed source for the user config (Claude only): a read-only bind at a
    # sidecar path outside ~/.claude/, copied into ~/.claude.json at create time
    # so cached feature-gate grants (e.g. Fable) survive. See
    # _CLAUDE_CONFIG_SEED_TARGET. Kept off the state volume on purpose — the seed
    # is re-applied from the host on every create, not persisted stale.
    if seeding_config:
        mounts.append(_bind_mount(config_seed_source, _CLAUDE_CONFIG_SEED_TARGET, readonly=True))
    # Persist the agent's home-state dir (plans, transcripts, todos) across
    # rebuilds. The creds bind is a sidecar OUTSIDE this dir for both apps, so the
    # volume owns the real credentials file (~/.claude/.credentials.json,
    # ~/.codex/auth.json) and the seeded copy lands on it writably.
    state_mount = state_volume_mount(app, project_id)
    if state_mount:
        mounts.append(state_mount)
    # User's HOST agent skills ([sandbox] mount_host_skills): one read-only bind
    # per skill at the sidecar (see HOST_SKILLS_SIDECAR). The host source is the
    # REAL directory (init resolves ~/.claude/skills symlinks before mounting),
    # so nothing dangles inside the container.
    for name, source in host_skills:
        mounts.append(_bind_mount(source, f"{HOST_SKILLS_SIDECAR}/{name}", readonly=True))
    for source, target in trusted_eda_mounts:
        mounts.append(_bind_mount(source, target, readonly=True))
    # Masks follow content mounts, but the immutable definition bind remains
    # last so no project-controlled over-mount can change the next creation.
    # binds above it, and mount order is what makes that stick (see
    # _mask_mounts — docker applies workspaceMount first, then this list in
    # order, so the masks must trail every mount they are meant to shadow).
    mounts.extend(_mask_mounts(mask_paths, mask_source))
    if protected_devcontainer_source:
        mounts.append(
            _bind_mount(
                protected_devcontainer_source,
                f"{WORK_DIR}/.devcontainer",
                readonly=True,
            )
        )
    return mounts


def _build_remote_env(
    app: str,
    forward_oauth_token: bool,
    local_timezone: str,
) -> dict[str, str]:
    """Build the ``remoteEnv`` dict for :func:`build_devcontainer_spec`."""
    remote_env = {
        # Egress is forced through the dual-homed proxy on the --internal network.
        "HTTP_PROXY": PROXY_URL,
        "HTTPS_PROXY": PROXY_URL,
        "http_proxy": PROXY_URL,
        "https_proxy": PROXY_URL,
        "NO_PROXY": "localhost,127.0.0.1",
        # Container-side MCP runs in interactive mode and finds config here.
        "BOOLEY_MCP_MODE": "interactive",
        "BOOLEY_PROJECT_DIR": PROJECT_DIR_TARGET,
        # The in-container registrar (postStartCommand) configures this app.
        "BOOLEY_AGENT_APP": app,
    }
    if local_timezone:
        remote_env[LOCAL_TIMEZONE_ENV] = local_timezone

    # Forward the app's rotation-free credential into in-container runs: Claude's
    # one-year `claude setup-token` token, or Codex's API key. Referenced via
    # ${localEnv:...} so the secret is read from the host at container-create time
    # and never baked into the on-disk spec. Gated on the caller having actually
    # found one (`booley auth` stores it; init only forwards when it resolves) so
    # an empty value can't shadow the mounted subscription credentials.
    credential = auth_token.credential_for_app(app)
    if forward_oauth_token and credential is not None:
        remote_env[credential.env_var] = f"${{localEnv:{credential.env_var}}}"

    return remote_env


def _build_run_args(memory: str) -> list[str]:
    """Build the ``runArgs`` list for :func:`build_devcontainer_spec`."""
    # --network wires egress; the label lets the reaper own this container.
    # --cap-drop ALL + no-new-privileges + a pids ceiling harden the runtime where
    # the agent actually runs against a hijacked agent (matching the retired
    # per-call sandbox's posture; see ARCHITECTURE.md#security--trust-model). They
    # cost nothing to the legitimate EDA stack, which ran under the same flags
    # pre-ADR-0028.
    run_args = [
        "--init",
        "--network",
        EGRESS_NETWORK,
        "--label",
        INTERACTIVE_ROLE_LABEL,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(SESSION_PIDS_LIMIT),
    ]
    if memory:
        # The single Session Runtime memory limit (ADR 0028 Decision 12).
        run_args += ["--memory", memory]
    return run_args


def _build_post_create_command(
    seeding_config: bool,
    seeding_creds: bool,
    auth_target: str | None,
    seed_source: str | None,
    mcp_start_command: str | None,
) -> str | None:
    """Build the ``postCreateCommand`` string for :func:`build_devcontainer_spec`.

    Runs once during creation, BEFORE the agent extension starts its first
    session — Claude Code reads ~/.claude.json only at session start, so both
    the config seed and MCP registration land before anything reads it. The
    seed MUST precede the registrar: incontainer_register's upsert_claude
    merges mcpServers.booley INTO the existing config, so seeding first yields
    host-gates + MCP; seeding after would merge into an empty file and lose the
    grants. ``cp -n`` never clobbers an already-populated config (e.g. a
    resumed container), and the config target is ephemeral so a rebuild
    re-seeds fresh from the host. The config seed stays a create-time concern;
    the credential seed does NOT — see :func:`_build_creds_seed_command`.
    """
    seed_cmd = (
        f"cp -n {_CLAUDE_CONFIG_SEED_TARGET} {_CLAUDE_CONFIG_TARGET} 2>/dev/null || true"
        if seeding_config
        else None
    )
    creds_seed_cmd = _build_creds_seed_command(seeding_creds, auth_target, seed_source)
    return "; ".join(c for c in (seed_cmd, creds_seed_cmd, mcp_start_command) if c) or None


def _build_creds_seed_command(
    seeding_creds: bool, auth_target: str | None, seed_source: str | None
) -> str | None:
    """Build the credential-seeding fragment shared by postCreate and postStart.

    Plain ``cp`` (not ``cp -n``): the host's OAuth token ROTATES, and a refresh
    on the host invalidates the copy the container is holding. The credential
    seed mount is a read-only bind of the host's credentials file, so
    re-copying it makes the host the single source of truth at every container
    entry point — not just at create.

    KNOWN LIMIT: a single-file bind pins the file's INODE at container start,
    and the host CLI rewrites credentials via atomic rename — so the seed goes
    stale the moment the host refreshes, and every later copy re-seeds that
    stale snapshot (whose refresh token the host rotation has revoked).
    Recreating the container re-binds the current inode. This is why ticket
    agents must not depend on the seeded file at all: the rotation-free
    ``booley auth`` token (injected via ``_build_sdk_options`` ``options.env``)
    is the credential that survives host refreshes.

    This runs on postStart as well as postCreate because credentials are the one
    piece of seeded state that goes STALE while a container merely sits stopped.
    A container created days ago and resumed with ``docker start`` used to keep
    its long-dead create-time token, and every agent session inside it failed
    with "Not logged in" — a whole benchmark run's worth of infra errors that
    looked like an agent bug. chmod 600 keeps the file agent-private (``cp``
    drops the source's mode). The in-container refresh takes over from there.
    (Unfixable edge: a host refresh token already dead at seed time can't be
    salvaged by any copy.)
    """
    if not seeding_creds or not seed_source:
        return None
    return f"(cp {seed_source} {auth_target} && chmod 600 {auth_target}) 2>/dev/null || true"


def build_devcontainer_spec(
    app: str = APP_NONE,
    *,
    image: str = SANDBOX_IMAGE,
    project_dir_source: str = DEFAULT_PROJECT_DIR_SOURCE,
    project_id: str = "${localWorkspaceFolderBasename}",
    auth_token_source: str | None = None,
    config_seed_source: str | None = None,
    mcp_start_command: str | None = None,
    memory: str = "",
    forward_oauth_token: bool = False,
    token_seed_source: str | None = None,
    host_skills: Sequence[tuple[str, str]] = (),
    trusted_eda_mounts: Sequence[tuple[str, str]] = (),
    protected_devcontainer_source: str = "",
    fixed_container_env: dict[str, str] | None = None,
    mask_paths: Sequence[str] = (),
    mask_source: str = "",
    local_timezone: str = "",
) -> dict:
    """Build the ``devcontainer.json`` dict for an Interactive Mode session.

    Args:
        app: one of :data:`SUPPORTED_APPS`; selects the agent extension and the
            auth-token target. ``"none"`` installs no agent extension.
        image: the prebuilt runtime image (``booley-sandbox`` by default; the
            published ref can be substituted by the caller).
        project_dir_source: host-side mount source for ``.booley_project``.
        project_id: canonical Project-root identity used to scope persistent
            container state. Production callers pass :func:`canonical_project_id`.
        auth_token_source: host path to the agent auth token to mount read-only;
            ``None`` mounts no token (e.g. ``app == "none"`` or API-key auth).
        config_seed_source: host path to ``~/.claude.json`` to seed the
            container's user config from (Claude only). Mounted read-only at a
            sidecar path and copied into ``~/.claude.json`` by
            ``postCreateCommand`` — before the MCP registrar — so cached
            feature-gate grants (staged-rollout / promo model access such as
            Fable), the stableID, and onboarding state carry over from the host
            instead of being re-rolled empty on every container create. The host
            file is never written (read-only + copy). ``None`` (or any non-Claude
            app) mounts no seed.
        mcp_start_command: the shell command for ``postStartCommand`` that starts
            the in-container MCP server (WS4). ``None`` omits the hook.
        memory: the ONE container memory limit (ADR 0028 Decision 12), e.g.
            ``"8g"``, from ``[sandbox] memory`` in booley.toml. Rendered as a
            ``--memory`` run arg; the empty default sets no limit — matching
            the pre-ADR-0028 devcontainer, so existing installs are unchanged.
        forward_oauth_token: when True (Claude app only), add a
            ``CLAUDE_CODE_OAUTH_TOKEN`` remoteEnv entry that references the host
            env (``${localEnv:...}``) so a ``claude setup-token`` credential
            reaches headless in-container runs. ``booley init`` sets this only
            when the var is present in the host env, so an empty value never
            shadows the mounted subscription credentials.
        token_seed_source: host path to the rotation-free credential stored by
            ``booley auth``, mounted read-only at the app's
            ``_APP_TOKEN_SEED_TARGET`` sidecar for ``incontainer_register`` to
            apply on every container start. This is the path that reaches VS
            Code's "Reopen in Container", where the remoteEnv ``${localEnv:...}``
            reference resolves empty (VS Code never sees the shell's exports).
            ``None`` mounts no sidecar.
        host_skills: ``(name, host_source)`` pairs for the user's HOST agent
            skills, from ``[sandbox] mount_host_skills`` (init resolves the
            real skill dirs behind ``~/.claude/skills`` / ``~/.agents/skills``,
            excluding Booley's own built-ins). Each rides in as its own
            read-only bind at ``<HOST_SKILLS_SIDECAR>/<name>``;
            ``incontainer_register`` links them into the agent's skills dir on
            start, built-ins winning any name clash. Empty (the default) mounts
            no host skills.
        mask_paths: workspace-root-relative POSIX paths from
            ``[sandbox] mask_paths`` to HIDE from the Session Runtime. Each is
            rendered as a read-only bind of *mask_source* over
            ``/work/<rel>`` — and, for a path under ``.booley_project/``, a
            second bind over ``/booley-project/<rest>``, because that tree is
            visible through both mounts (see :func:`_mask_mounts`). Callers
            validate via :func:`mask_paths_error` first. Empty (the default)
            masks nothing and leaves the spec byte-identical to before the
            knob existed.
        mask_source: host path of the dedicated ALWAYS-EMPTY directory the
            mask binds use as their source (``booley init`` creates it under
            Booley's per-user config dir). Required whenever *mask_paths* is
            non-empty — the builder stays pure and never mkdirs, so the
            caller owns creating the dir before the container is created.
        local_timezone: host IANA timezone name (or fixed ``+HH:MM`` fallback)
            passed into the runtime so human-visible timestamps use the user's
            local time rather than the container image's UTC default.

    The spec uses only Booley's fixed ``initializeCommand``.  It validates the
    host-issued spec and, when licensed EDA is granted, prepares the fixed relay
    topology before Dev Containers asks Docker to create the runtime.  No
    Project-controlled command or argument participates in that host action.
    """
    if app not in SUPPORTED_APPS:
        raise ValueError(f"unknown app {app!r}; expected one of {SUPPORTED_APPS}")
    if mask_paths and not mask_source:
        # Loud, not silent: dropping the masks here would hand the agent the
        # exact visibility the project configured away.
        raise ValueError("mask_paths given without mask_source (the empty host dir to bind)")

    auth_target = _auth_target_for_app(app)
    # Both apps refresh their credential file in-container, so both need the
    # sidecar-seed + writable-copy treatment (see _APP_CREDS_SEED_TARGET).
    seeding_creds = bool(auth_token_source) and app in _APP_CREDS_SEED_TARGET
    seeding_config = bool(config_seed_source) and app == APP_CLAUDE

    mounts = _build_mounts(
        app,
        project_id,
        project_dir_source,
        auth_token_source,
        auth_target,
        seeding_creds,
        config_seed_source,
        seeding_config,
        token_seed_source,
        host_skills,
        trusted_eda_mounts,
        protected_devcontainer_source,
        mask_paths,
        mask_source,
    )
    remote_env = _build_remote_env(app, forward_oauth_token, local_timezone)
    run_args = _build_run_args(memory)

    spec: dict = {
        "name": f"Booley Interactive ({app})",
        "image": image,
        "workspaceMount": _bind_mount("${localWorkspaceFolder}", WORK_DIR),
        "workspaceFolder": WORK_DIR,
        "remoteUser": "agent",
        "runArgs": run_args,
        "mounts": mounts,
        "remoteEnv": remote_env,
        "initializeCommand": [
            "booley",
            "session",
            "prepare",
            "--project-root",
            "${localWorkspaceFolder}",
        ],
        # Survive window close (ADR 0028 Decision 11): tickets may still be
        # running when the last VS Code window disconnects, so VS Code must
        # never stop the container itself — the idle reaper is the sole
        # lifecycle owner (same idle-timeout + session cap as before).
        "shutdownAction": "none",
        # The issued image ID is immutable.  Letting Dev Containers synthesize
        # an update-UID derivative would both fail for an ID-only FROM and run
        # an image that the host did not inspect or stamp.
        "updateRemoteUserUID": False,
    }
    if fixed_container_env:
        spec["containerEnv"] = dict(fixed_container_env)

    seed_source = _creds_seed_target_for_app(app)
    post_create = _build_post_create_command(
        seeding_config, seeding_creds, auth_target, seed_source, mcp_start_command
    )
    if post_create:
        spec["postCreateCommand"] = post_create
    # Re-seed credentials on every start, then revive the MCP endpoint: a resumed
    # container must not run agents against the token it froze at create time.
    post_start = "; ".join(
        c
        for c in (
            _build_creds_seed_command(seeding_creds, auth_target, seed_source),
            mcp_start_command,
        )
        if c
    )
    if post_start:
        spec["postStartCommand"] = post_start

    # The Waveform Viewer (ADR 0035, VaporView) rides along for every app —
    # including "none": viewing a trace isn't tied to which app is attached —
    # so the customizations block is unconditional now.
    extension = _vscode_extension_for_app(app)
    vscode: dict = {
        "extensions": ([extension] if extension else [])
        + [
            _VAPORVIEW_EXTENSION,
            _HDL_EXTENSION,
            _TCL_EXTENSION,
            _LIVE_PREVIEW_EXTENSION,
        ],
        # VaporView's own manifest already declares extensionKind
        # ["workspace"], so it is not pinned here.
        "settings": {
            "vaporview.wcp.enabled": True,
            "vaporview.wcp.port": _VAPORVIEW_WCP_PORT,
            "files.associations": dict(_HIGHLIGHT_FILE_ASSOCIATIONS),
            **_LIVE_PREVIEW_SETTINGS,
            **_PYTHON_TERMINAL_SETTINGS,
        },
    }
    if extension:
        # Pin the agent extension workspace-side (reinforces the
        # container-install above; enforcement is client-side).
        vscode["settings"]["remote.extensionKind"] = {extension: ["workspace"]}
    spec["customizations"] = {"vscode": vscode}

    # VaporView's WCP server does not auto-start from the settings above alone
    # (the setting is application-scoped, so the container's Machine settings
    # are ignored, and the extension activates lazily). The post-attach patcher
    # fixes both in the installed manifest — attach is the first lifecycle point
    # where that manifest exists on disk. Unconditional: viewing traces is not
    # tied to which agent app is attached, and the hook only fires when a UI
    # client attaches, so headless ticket containers skip it for free.
    spec["postAttachCommand"] = post_attach_command()

    return spec


def render_devcontainer_json(spec: dict) -> str:
    """Render *spec* as the on-disk ``devcontainer.json`` text (2-space, newline)."""
    return json.dumps(spec, indent=2) + "\n"


def devcontainer_path(folder: Path) -> Path:
    """Return the ``.devcontainer/devcontainer.json`` path under *folder*."""
    return folder / ".devcontainer" / "devcontainer.json"


def write_devcontainer(folder: Path, spec: dict) -> Path:
    """Write *spec* to ``<folder>/.devcontainer/devcontainer.json``; return the path.

    Overwrites Booley's own generated spec (it is regenerated, treated read-only
    within a session). Refusing a *tracked* ``.devcontainer/`` is the caller's
    job (``booley init`` / ``booley doctor``), not this writer's.
    """
    path = devcontainer_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_devcontainer_json(spec), encoding="utf-8")
    return path
