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

import json
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from booley.config.agent import SANDBOX_IMAGE
from booley.core.project_identity import project_name_id
from booley.runtime import auth_token
from booley.runtime import vaporview as _vaporview
from booley.runtime.compiler_cache import COMPILER_CACHE_ROOT_ENV, ISSUED_COMPILER_CACHE_ROOT
from booley.runtime.guarded_write import replace_regular_file
from booley.runtime.incontainer_git_identity import (
    GitIdentity,
    GitIdentityError,
    git_identity_environment,
    load_git_identity,
)
from booley.runtime.sandbox_layout import BOOLEY_DIR_TARGET, WORK_DIR
from booley.runtime.timefmt import LOCAL_TIMEZONE_ENV
from booley.runtime.vaporview import PRESENTATION_COLOR_SETTINGS

# --- Supported agent apps (mirrors the init wizard's app selection) ---
APP_CLAUDE = "claude"
APP_CODEX = "codex"
APP_NONE = "none"
SUPPORTED_APPS = (APP_CLAUDE, APP_CODEX, APP_NONE)

# --- Long-lived Docker objects created by ``booley init`` (WS1/WS2) ---
# The Sandbox attaches to this --internal network; the dual-homed
# ``booley-proxy`` is its sole egress path.
# Versioned because the original ``booley-egress`` used Docker's default
# internal-bridge gateway, which remained reachable from Sandboxes.
# A distinct name lets ``booley init`` migrate without disrupting an already
# running legacy Session; new specs can only attach to the host-isolated v2.
EGRESS_NETWORK = "booley-egress-v2"
PROXY_HOST = "booley-proxy"
PROXY_PORT = 8080
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
# The routed user-defined network every Sandbox joins under host
# ``[sandbox] egress = "unproxied"``. User-defined so it can sit next to the
# internal license network, which Docker's default ``bridge`` cannot.
UNPROXIED_NETWORK = "booley-unproxied-v1"

# Label the idle reaper (WS2) matches to find/own Sandboxes.
INTERACTIVE_ROLE_LABEL = "booley.role=interactive"

# Process cap for the whole Sandbox (fork-bomb ceiling, not a tuning
# knob). The container hosts the editor, agent clients, and concurrent EDA
# tools (ADR 0028). The cap bounds a hijacked agent's
# fork bomb; legitimate peak process counts stay well under this.
SESSION_PIDS_LIMIT = 4096

# --- Container-side paths (must match the booley-sandbox image) ---
# BOOLEY_DIR_TARGET (imported above from ``sandbox_layout``) is where
# ``.booley`` mounts; it is re-exported here for existing callers.
AGENT_HOME = "/home/agent"  # non-root ``agent`` user (see data/docker/Dockerfile)

# Workspace-relative basename of the Booley Directory. ``[sandbox] mask_paths``
# needs it to notice entries that are visible through TWO container mounts:
# the whole workspace binds at /work (so ``.booley/x`` shows up at
# ``/work/.booley/x``) AND the Booley Directory itself binds again at
# BOOLEY_DIR_TARGET — masking only the /work view would leave the same bytes
# readable at ``/booley-project/x``.
BOOLEY_DIR_BASENAME = ".booley"

# Default mount source for ``.booley``: the co-located common case.
# The Dev Containers CLI does NOT support nested variable defaults
# (``${localEnv:VAR:${localWorkspaceFolder}/...}`` is mis-parsed into an invalid
# mount path), so for non-co-located projects ``booley init`` bakes the resolved
# absolute host path instead of relying on env-var indirection here.
DEFAULT_BOOLEY_DIR_SOURCE = "${localWorkspaceFolder}/.booley"

# Waveform Viewer (ADR 0078): VaporView is installed for every app so
# `bwave gui` works in the attached window, driven through the Booley
# extension's Viewer Bridge. VaporView has no named red color; `bwave gui`
# maps its stable red/blue/green interface to the extension's custom palette.
_VAPORVIEW_PRESENTATION_SETTINGS = PRESENTATION_COLOR_SETTINGS
# Verilog/SystemVerilog syntax highlighting: without an HDL grammar the
# attached window renders every .v/.sv file as plain text. mshr-h's extension
# ships the TextMate grammar (plus ctags-based navigation) and keeps linting
# OFF by default, so it adds highlighting without competing with Booley's own
# lint flow. Installed for every app — reading RTL isn't tied to which agent
# is attached. Like VaporView it is the pinned VSIX bundled in the Sandbox
# Image and installed by path: an egress-locked Sandbox cannot reach a gallery,
# and VS Code pins a VSIX install so auto-update leaves it there.
# ``Dockerfile.base`` carries the same version (tests enforce the lockstep).
_HDL_VERSION = "1.29.0"
HDL_VSIX = f"/usr/local/libexec/booley/veriloghdl-{_HDL_VERSION}.vsix"
# The Marketplace ID older specs listed; Doctor flags a spec that still does.
_HDL_GALLERY_ID = "mshr-h.veriloghdl"

# VS Code entries older Booley specs installed and no current spec carries:
# the Claude Code / Codex agent extensions (the agent CLI is the only agent
# surface), the Tcl grammar, and the *.sdc/*.xdc -> tcl associations (the HDL
# extension claims those suffixes itself). Doctor flags a spec still listing them.
_RETIRED_AGENT_EXTENSIONS = ("Anthropic.claude-code", "openai.chatgpt")
_RETIRED_EXTENSIONS = ("bitwisecook.tcl", *_RETIRED_AGENT_EXTENSIONS)
_RETIRED_FILE_ASSOCIATIONS = {"*.sdc": "tcl", "*.xdc": "tcl"}

# Microsoft's Live Preview hosts static HTML and renders it in an embedded
# VS Code tab. Installed for every app so human-facing reports (including the
# review HTML under the separately mounted Booley Directory) are viewable
# without a browser in the sandbox. Bundled in the Sandbox Image and installed
# by path, like the HDL extension above.
_LIVE_PREVIEW_VERSION = "0.4.20"
LIVE_PREVIEW_VSIX = f"/usr/local/libexec/booley/live-server-{_LIVE_PREVIEW_VERSION}.vsix"
_LIVE_PREVIEW_GALLERY_ID = "ms-vscode.live-server"

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

# The Sandbox installs the framework and its Python dependencies system-wide;
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
# the in-container ``/model`` picker even when the CLI and
# credentials match the host. We seed the container's copy from the host's
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
# refresh write FAIL — and in a headless Sandbox nothing on the host
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
# (see :func:`booley.runtime.incontainer_setup.apply_stored_credential`), so every
# entry point — VS Code, ``booley session``, headless drivers — sees the same
# credential with no manual export. Same home-sidecar placement rationale as
# ``_APP_CREDS_SEED_TARGET`` above.
_APP_TOKEN_SEED_TARGET = {
    app: f"{AGENT_HOME}/{name}" for app, name in auth_token.TOKEN_SEED_BASENAME.items()
}

# Sidecar root for the user's HOST agent skills ([sandbox] mount_host_skills).
# Each host skill dir rides in as its own read-only bind at
# ``<sidecar>/<name>``; ``incontainer_register.deploy_host_skills`` then
# symlinks each into the app's real skills dir (``~/.claude/skills`` /
# ``~/.agents/skills``) on every container start, AFTER the built-ins so a
# built-in of the same name always wins. Kept OUTSIDE the app state dir (same
# state-volume-nesting rationale as the credential sidecars above), and the
# read-only bind makes the whole tree unwritable, so a mounted host skill can
# never be mutated from inside the sandbox.
HOST_SKILLS_SIDECAR = f"{AGENT_HOME}/.booley-host-skills"


def git_identity_command() -> str:
    """Fixed command that cleans recognized legacy Interactive identity overrides."""
    return "python -m booley.runtime.incontainer_git_identity_cleanup"


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
    return "python -m booley.harness.incontainer_register"


def vaporview_patch_command() -> str:
    """Shell command for ``postAttachCommand``: apply Booley's residual VaporView patch.

    Runs on *attach* (not create/start) because it edits the installed
    VaporView extension, which VS Code installs container-side only once a
    window attaches — by postCreate/postStart it isn't on disk yet. The
    patcher skips VaporView's remote desktop-theme lookup and makes the custom
    palette slots behind `bwave gui` colors honor the spec's Machine settings.
    If installation outlasts the short attach wait, it leaves a singleton
    watcher until the configured extension arrives, including patching VS
    Code's staging copy before publication. Idempotent and never fails the
    hook. See :mod:`booley.runtime.incontainer_vaporview` for the rationale.
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
    """Stable collision-resistant identity for one canonical Project Root."""
    return project_name_id(str(project_root.resolve(strict=True)))


def state_volume_name(app: str, project_id: str, policy: str = "auto") -> str | None:
    """Name of the persistent home-state volume for *app* and *project_id*.

    Returns ``None`` for apps without a persisted state dir. The single format
    source so the spec builder and ``booley doctor`` agree on names.
    """
    if app not in _APP_STATE_DIR:
        return None
    suffix = "" if policy == "auto" else f"-{policy}"
    return f"booley-{app}-state-{project_id}{suffix}"


def state_volume_names(project_id: str) -> frozenset[str]:
    """Every state-volume name one Project can own across agent apps and auth policies."""
    return frozenset(
        name
        for app in _APP_STATE_DIR
        for policy in ("auto", "subscription", "api_key")
        if (name := state_volume_name(app, project_id, policy)) is not None
    )


def state_volume_mount(app: str, project_id: str, policy: str = "auto") -> str | None:
    """Return the persistent home-state volume mount for *app*, or ``None``.

    Per-project + per-app naming keeps projects isolated: every interactive
    container mounts the workspace at the same ``/work``, so a shared volume
    would collide their session transcripts.
    """
    target = _APP_STATE_DIR.get(app)
    if target is None:
        return None
    name = state_volume_name(app, project_id, policy)
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
    """Whether *spec* installs VaporView and the Booley bridge that drives it.

    The Waveform Viewer (ADR 0078) reaches an attached VS Code window only
    through the generated spec: the image-bundled VaporView VSIX install,
    the image-local Booley Sandbox Integration extension that hosts the Viewer Bridge, the
    semantic waveform-color settings, AND the ``postAttachCommand`` patch
    that makes those colors effective. A spec seeded before any of these
    landed leaves scoped ``bwave gui`` without a usable viewer — even after an
    image rebuild, which never touches the spec. ``booley doctor`` reads this
    to surface the drift.

    Returns ``True`` only when VaporView is listed exactly once, as the
    bundled ``vaporview.BUNDLED_VSIX`` path (any gallery ID is drift: an
    egress-locked Sandbox cannot install it), the Booley Sandbox Integration extension is listed, the
    presentation palette is intact, AND the patch is wired; any miss means a
    stale or hand-edited spec that a refresh would fix.
    """
    from booley.runtime.dashboard_extension import VSIX_PATH

    customizations = spec.get("customizations")
    vscode = customizations.get("vscode") if isinstance(customizations, dict) else None
    if not isinstance(vscode, dict):
        return False
    extensions = vscode.get("extensions")
    if not isinstance(extensions, list) or VSIX_PATH not in extensions:
        return False
    # Exactly one VaporView entry, the bundled VSIX: a gallery entry (pinned or
    # not) never installs in an egress-locked Sandbox, and another VSIX path
    # names a build Booley has not verified.
    requested = [entry for entry in extensions if _vaporview.is_vaporview_entry(entry)]
    if requested != [_vaporview.BUNDLED_VSIX]:
        return False
    settings = vscode.get("settings")
    if not isinstance(settings, dict):
        return False
    if not all(
        settings.get(key) == value for key, value in _VAPORVIEW_PRESENTATION_SETTINGS.items()
    ):
        return False
    return _post_attach_has(spec, vaporview_patch_command())


def _installs_only_bundled(extensions: list, gallery_id: str, vsix: str) -> bool:
    """Whether *extensions* requests an extension exactly once, as the bundled *vsix*.

    Counts the entries that equal *vsix* or name the extension's *gallery_id*
    (any ``@version``, any case). A gallery ID is drift: an egress-locked
    Sandbox cannot install it. A stale bundled path left from an earlier image
    version is drift too, since it leaves the current *vsix* unrequested.
    """
    requested = [
        entry
        for entry in extensions
        if isinstance(entry, str)
        and (entry == vsix or entry.lower().partition("@")[0] == gallery_id)
    ]
    return requested == [vsix]


def spec_installs_hdl_highlight(spec: dict) -> bool:
    """Whether *spec* installs the Verilog/SystemVerilog highlighting extension.

    The grammar reaches an attached window only through the generated spec's
    ``customizations``, as the VSIX bundled in the Sandbox Image
    (:data:`HDL_VSIX`). A spec that lacks it, or that lists the gallery ID
    instead, renders RTL as plain text until a re-seed.
    The extension also claims the ``.sdc``/``.xdc`` suffixes (checked against
    mshr-h.veriloghdl 1.29.0, 2026-10), so nothing else is required.
    ``booley doctor`` reads this to surface the drift.
    """
    customizations = spec.get("customizations")
    vscode = customizations.get("vscode") if isinstance(customizations, dict) else None
    if not isinstance(vscode, dict):
        return False
    extensions = vscode.get("extensions")
    return isinstance(extensions, list) and _installs_only_bundled(
        extensions, _HDL_GALLERY_ID, HDL_VSIX
    )


def spec_retired_vscode_entries(spec: dict) -> list[str]:
    """Name the retired VS Code entries *spec* still carries, in a stable order.

    Lists each retired extension ID in ``extensions``, each retired
    ``files.associations`` pattern still routed to Tcl, and each retired agent
    extension still pinned in ``remote.extensionKind``. A spec
    with unexpected shapes reports nothing for the malformed part, like the
    sibling ``spec_*`` detectors. ``booley doctor`` reads this to surface specs
    written before those entries were dropped.
    """
    customizations = spec.get("customizations")
    vscode = customizations.get("vscode") if isinstance(customizations, dict) else None
    if not isinstance(vscode, dict):
        return []
    found: list[str] = []
    extensions = vscode.get("extensions")
    if isinstance(extensions, list):
        found += [ext for ext in _RETIRED_EXTENSIONS if ext in extensions]
    settings = vscode.get("settings")
    if not isinstance(settings, dict):
        return found
    associations = settings.get("files.associations")
    if isinstance(associations, dict):
        found += [
            f"files.associations {pattern}"
            for pattern, lang in _RETIRED_FILE_ASSOCIATIONS.items()
            if associations.get(pattern) == lang
        ]
    pin = settings.get("remote.extensionKind")
    if isinstance(pin, dict):
        found += [f"remote.extensionKind {ext}" for ext in _RETIRED_AGENT_EXTENSIONS if ext in pin]
    return found


def spec_installs_live_preview(spec: dict) -> bool:
    """Whether *spec* installs and safely configures the HTML report viewer.

    Live Preview is the VSIX bundled in the Sandbox Image and installed by
    path from the devcontainer spec (:data:`LIVE_PREVIEW_VSIX`); a gallery ID
    is drift, since an egress-locked Sandbox cannot install it. Restored remote-port tunnels can retain Live Preview's
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
        and _installs_only_bundled(extensions, _LIVE_PREVIEW_GALLERY_ID, LIVE_PREVIEW_VSIX)
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
    Sandbox (each becomes a read-only bind of an always-empty host dir
    over ``/work/<rel>``, see :func:`_mask_mounts`). Only clean relative
    subpaths are accepted: an absolute path would mask an arbitrary container
    path and a ``..`` segment would walk the mask outside the workspace mount —
    either turns a privacy knob into a container-layout editor. Masking
    ``.booley`` itself is rejected too: the session reads its config,
    Goal Records, and worktrees through that mount (``BOOLEY_DIR``), so
    hiding the whole dir bricks the runtime — mask *subtrees* of it instead.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return "[sandbox].mask_paths must be a list of strings"
    bad: list[str] = []
    whole_booley_dir: list[str] = []
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
        elif parts == (BOOLEY_DIR_BASENAME,):
            whole_booley_dir.append(v)
    if bad:
        return (
            "[sandbox].mask_paths entries must be workspace-relative POSIX "
            "paths with no '..' segments: " + ", ".join(repr(v) for v in bad)
        )
    if whole_booley_dir:
        return (
            f"[sandbox].mask_paths must not mask {BOOLEY_DIR_BASENAME!r} "
            "itself — the session reads its config through that mount; mask "
            f"subtrees (e.g. {BOOLEY_DIR_BASENAME}/private) instead"
        )
    return None


def _mask_mounts(mask_paths: Sequence[str], mask_source: str) -> list[str]:
    """Read-only empty binds hiding ``[sandbox] mask_paths`` subtrees.

    Each entry becomes a bind of *mask_source* — a dedicated ALWAYS-EMPTY host
    dir — over ``/work/<rel>``, so the subtree simply reads as an empty dir in
    the Sandbox. Since ADR 0028 every agent is an in-container
    subprocess sharing the one workspace mount (no per-agent mounts), an
    over-mount is the only per-path visibility control left. Two deliberate
    choices:

    - A bind of an empty dir, NOT a tmpfs mount: the Dev Containers CLI mount
      schema does not reliably support tmpfs entries, and an empty read-only
      bind behaves identically for hiding purposes.
    - ``readonly``: the mask must also swallow writes — an agent writing into
      a masked subtree would otherwise scribble into the shared empty dir.

    An entry under ``.booley/`` is masked a SECOND time under
    ``/booley-project/<rest>``: the Booley Directory is bind-mounted twice (the
    workspace mount at /work plus its own mount at BOOLEY_DIR_TARGET), and
    masking only one view leaves the same bytes readable through the other.

    Callers must place these AFTER the Booley Directory mount in the ``mounts``
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
        if parts[0] == BOOLEY_DIR_BASENAME and len(parts) > 1:
            rest = "/".join(parts[1:])
            mounts.append(_bind_mount(mask_source, f"{BOOLEY_DIR_TARGET}/{rest}", readonly=True))
    return mounts


def _build_mounts(
    app: str,
    project_id: str,
    booley_dir_source: str,
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
    mounts = [_bind_mount(booley_dir_source, BOOLEY_DIR_TARGET)]
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
    egress: str,
) -> dict[str, str]:
    """Build the ``remoteEnv`` dict for :func:`build_devcontainer_spec`."""
    remote_env = (
        {
            # Egress is forced through the dual-homed proxy on the --internal network.
            "HTTP_PROXY": PROXY_URL,
            "HTTPS_PROXY": PROXY_URL,
            "http_proxy": PROXY_URL,
            "https_proxy": PROXY_URL,
            "NO_PROXY": "localhost,127.0.0.1",
        }
        if egress == "proxy"
        else {}
    )
    remote_env |= {
        # Container-side MCP runs in interactive mode and finds config here.
        "BOOLEY_MCP_MODE": "interactive",
        "BOOLEY_DIR": BOOLEY_DIR_TARGET,
        COMPILER_CACHE_ROOT_ENV: ISSUED_COMPILER_CACHE_ROOT,
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


def _build_run_args(memory: str, egress: str) -> list[str]:
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
        EGRESS_NETWORK if egress == "proxy" else UNPROXIED_NETWORK,
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
        # The single Sandbox memory limit (ADR 0028 Decision 12).
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

    Runs once during creation, BEFORE the agent CLI starts its first
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
    return "; ".join(
        command
        for command in (seed_cmd, creds_seed_cmd, _runtime_start_tail(mcp_start_command))
        if command
    )


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
    Recreating the container re-binds the current inode. Agent calls must not depend on the seeded file at all: the rotation-free
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


def _runtime_start_tail(mcp_start_command: str | None) -> str:
    """Clean legacy identity before registration, preserving cleanup failure."""
    identity = git_identity_command()
    return f"{identity} && {mcp_start_command}" if mcp_start_command else identity


def _git_environment(project_source: str | None, fixed: dict[str, str] | None) -> dict[str, str]:
    environment = git_identity_environment(GitIdentity("Dev", "dev@localhost"))
    supplied = {key: value for key, value in (fixed or {}).items() if key in environment}
    if supplied:
        expected = git_identity_environment(
            GitIdentity(
                supplied.get("GIT_CONFIG_VALUE_0", ""), supplied.get("GIT_CONFIG_VALUE_1", "")
            )
        )
        if supplied != expected:
            raise GitIdentityError("[agent.git] fixed Git environment is incomplete or malformed")
        return expected
    if project_source:
        return git_identity_environment(load_git_identity(Path(project_source)))
    return environment


def _policy_sources(
    app: str, plan: auth_token.CredentialPlan | None, login: str | None, token: str | None
) -> tuple[str | None, str | None]:
    if plan is None:
        return login, token
    if plan.app != app:
        raise ValueError("credential plan provider differs from Sandbox app")
    return login if plan.subscription_seed else None, token if plan.token_seed else None


def _policy_state_mounts(
    app: str, project_id: str, mounts: list[str], plan: auth_token.CredentialPlan | None
) -> list[str]:
    if plan is None:
        return mounts
    old_state = state_volume_mount(app, project_id)
    new_state = state_volume_mount(app, project_id, plan.policy)
    return [str(new_state) if mount == old_state else mount for mount in mounts]


def _policy_remote_environment(
    remote: dict[str, str], plan: auth_token.CredentialPlan | None
) -> None:
    remote["BOOLEY_PRIMARY_AUTH"] = plan.policy if plan else "auto"
    remote["BOOLEY_CREDENTIAL_SCHEMA"] = auth_token.CREDENTIAL_SCHEMA
    if plan is not None:
        for key in auth_token.CREDENTIAL_ENV_KEYS:
            remote.pop(key, None)
        remote.update({key: f"${{localEnv:{key}}}" for key, _ in plan.values})


def build_devcontainer_spec(
    app: str = APP_NONE,
    *,
    credential_plan: auth_token.CredentialPlan | None = None,
    image: str = SANDBOX_IMAGE,
    booley_dir_source: str = DEFAULT_BOOLEY_DIR_SOURCE,
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
    dashboard: bool = False,
    egress: str = "proxy",
) -> dict:
    """Build the ``devcontainer.json`` dict for an Interactive Mode session.

    Args:
        app: one of :data:`SUPPORTED_APPS`; selects the auth-token target, seeds
            and state volume. ``"none"`` configures no agent.
        image: the prebuilt Sandbox Image (``booley-sandbox`` by default; the
            published ref can be substituted by the caller).
        booley_dir_source: host-side mount source for ``.booley``.
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
            ``[sandbox] mask_paths`` to HIDE from the Sandbox. Each is
            rendered as a read-only bind of *mask_source* over
            ``/work/<rel>`` — and, for a path under ``.booley/``, a
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
        egress: host ``[sandbox] egress`` mode. ``"proxy"`` (the default)
            joins :data:`EGRESS_NETWORK` with the proxy environment and leaves
            the spec byte-identical to before the setting existed;
            ``"unproxied"`` joins :data:`UNPROXIED_NETWORK`, sets no proxy
            environment, and declares ``BOOLEY_EGRESS=unproxied`` in
            ``containerEnv`` so bare ``docker exec`` calls see it too.

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

    auth_token_source, token_seed_source = _policy_sources(
        app, credential_plan, auth_token_source, token_seed_source
    )
    auth_target = _auth_target_for_app(app)
    # Both apps refresh their credential file in-container, so both need the
    # sidecar-seed + writable-copy treatment (see _APP_CREDS_SEED_TARGET).
    seeding_creds = bool(auth_token_source) and app in _APP_CREDS_SEED_TARGET
    seeding_config = bool(config_seed_source) and app == APP_CLAUDE

    mounts = _build_mounts(
        app,
        project_id,
        booley_dir_source,
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
    mounts = _policy_state_mounts(app, project_id, mounts, credential_plan)
    identity_env = _git_environment(booley_dir_source, fixed_container_env)
    remote_env = _build_remote_env(app, forward_oauth_token, local_timezone, egress)
    _policy_remote_environment(remote_env, credential_plan)
    remote_env.update(identity_env)
    run_args = _build_run_args(memory, egress)

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
            "--project",
            "${localWorkspaceFolder}",
        ],
        # Survive window close (ADR 0028 Decision 11): Jobs may still be
        # running when the last VS Code window disconnects, so VS Code must
        # never stop the container itself — the idle reaper is the sole
        # lifecycle owner for idle shutdown; admission enforces the host cap
        # before VS Code starts another Sandbox.
        "shutdownAction": "none",
        # The issued image ID is immutable.  Letting Dev Containers synthesize
        # an update-UID derivative would both fail for an ID-only FROM and run
        # an image that the host did not inspect or stamp.
        "updateRemoteUserUID": False,
    }
    spec["containerEnv"] = {
        **(fixed_container_env or {}),
        **identity_env,
        COMPILER_CACHE_ROOT_ENV: ISSUED_COMPILER_CACHE_ROOT,
    }
    if egress == "unproxied":
        spec["containerEnv"]["BOOLEY_EGRESS"] = "unproxied"

    seed_source = _creds_seed_target_for_app(app)
    post_create = _build_post_create_command(
        seeding_config, seeding_creds, auth_target, seed_source, mcp_start_command
    )
    if post_create:
        spec["postCreateCommand"] = post_create
    # Re-seed credentials on every start, then revive the MCP endpoint: a resumed
    # container must not run agents against the token it froze at create time.
    post_start = "; ".join(
        command
        for command in (
            _build_creds_seed_command(seeding_creds, auth_target, seed_source),
            _runtime_start_tail(mcp_start_command),
        )
        if command
    )
    if post_start:
        spec["postStartCommand"] = post_start

    # The Waveform Viewer (ADR 0078, VaporView) rides along for every app —
    # including "none": viewing a trace isn't tied to which app is attached —
    # so the customizations block is unconditional now.
    vscode: dict = {
        "extensions": [
            # Builds bundled in the image, installed by path: the egress-locked
            # Sandbox cannot reach a gallery, and VS Code pins a VSIX install
            # so auto-update leaves it there.
            _vaporview.BUNDLED_VSIX,
            HDL_VSIX,
            LIVE_PREVIEW_VSIX,
        ],
        "settings": {
            **_VAPORVIEW_PRESENTATION_SETTINGS,
            **_LIVE_PREVIEW_SETTINGS,
            **_PYTHON_TERMINAL_SETTINGS,
        },
    }
    from booley.runtime.dashboard_extension import VSIX_PATH

    # The image-local Booley Sandbox Integration extension hosts the `bwave gui` Viewer Bridge, so
    # every Sandbox installs it; the attach protocol alone decides whether it
    # also opens the Dashboard terminal.
    vscode["extensions"].append(VSIX_PATH)
    vscode["settings"]["booley.dashboard.attachProtocol"] = 1 if dashboard else 0
    spec["customizations"] = {"vscode": vscode}

    # The residual VaporView patch (theme lookup, palette scope) edits the
    # installed extension, so it runs at attach, the first lifecycle point
    # where that extension exists on disk. Unconditional: viewing traces is not
    # tied to which agent app is attached, and the hook only fires when a UI
    # client attaches, so headless Sandboxes skip it for free.
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
    job (``booley init`` / ``booley doctor``), not this writer's. The write is
    atomic and raises ``SymlinkTargetError`` for a symlinked spec file.
    """
    path = devcontainer_path(folder)
    replace_regular_file(path, render_devcontainer_json(spec).encode("utf-8"))
    return path
