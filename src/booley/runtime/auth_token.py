"""Long-lived agent credentials: where they live, and which one an app uses.

Both supported agent apps have the same failure mode, for the same reason: the
credential they ship with by default is a REFRESHING one, and a container is a
second holder of it.

* **Claude** (``~/.claude/.credentials.json``) holds an OAuth access token plus a
  refresh token, and refreshing ROTATES them. Host and container hold copies of
  the same refresh token, so whichever refreshes first silently revokes the
  other's — a long-running container suddenly fails every call with "Not logged
  in". There is no safe way to automate a shared refresh: a bidirectional sync is
  a race with no recovery.
* **Codex** (``~/.codex/auth.json``) holds a static API key *only* when signed in
  with one. A ChatGPT-subscription login stores refreshing OAuth tokens in the
  same file, and Codex rewrites the file when they refresh.

The rotation-free credential is therefore different per app, and this module owns
the mapping:

* Claude: a one-year token from ``claude setup-token`` (``CLAUDE_CODE_OAUTH_TOKEN``).
  It never refreshes, so nothing can rotate out from under a long run.
* Codex: an API key (``OPENAI_API_KEY``). Codex has no ``setup-token`` equivalent,
  so the API key is its only credential that cannot be revoked by a refresh
  elsewhere. It bills per token rather than against a subscription — that is a
  real trade-off, not a free win, and callers should say so.

Storage is OUTSIDE every repository and every bind mount
(``~/.config/booley/``, mode 0600) rather than baked into the generated
``.devcontainer/devcontainer.json``. A long-lived credential inside a project
directory is one ``git add -f`` away from being published, and the
``.git/info/exclude`` that hides the spec does not travel with a clone. Booley
reads the file and injects the value itself; the on-disk spec keeps the
``${localEnv:...}`` indirection and stays secret-free.

Resolution order everywhere: **environment variable first, stored file second.**
An explicitly exported value always wins, so a shell can override the stored one
without deleting it.

The invariant every caller must preserve: an empty or unset credential must never
shadow the mounted subscription credentials. Hence :func:`resolve_token` returns
``None`` — never an empty string — when there is nothing usable.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

APP_CLAUDE = "claude"
APP_CODEX = "codex"


@dataclass(frozen=True)
class AppCredential:
    """The rotation-free credential for one agent app."""

    app: str
    env_var: str
    filename: str
    # The command that mints the credential, or None when the app has no
    # equivalent of `claude setup-token` and the value must be supplied by hand.
    mint_cmd: tuple[str, ...] | None
    label: str
    # Prefix of a well-formed value; used only to warn on an obvious paste error,
    # never to hard-reject (a future prefix change must not lock anyone out).
    prefix: str


CREDENTIALS: dict[str, AppCredential] = {
    APP_CLAUDE: AppCredential(
        app=APP_CLAUDE,
        env_var="CLAUDE_CODE_OAUTH_TOKEN",
        filename="claude-oauth-token",
        mint_cmd=("claude", "setup-token"),
        label="one-year OAuth token",
        prefix="sk-ant-",
    ),
    APP_CODEX: AppCredential(
        app=APP_CODEX,
        env_var="OPENAI_API_KEY",
        filename="openai-api-key",
        # Codex has no `setup-token`: `codex login` writes a REFRESHING credential
        # into ~/.codex/auth.json, which is exactly what we are trying to avoid
        # depending on for a long run. The API key must be pasted in.
        mint_cmd=None,
        label="API key",
        prefix="sk-",
    ),
}

# Back-compat alias for the Claude env var name, which several call sites name
# directly (it is the one referenced in the generated devcontainer spec).
ENV_VAR = CREDENTIALS[APP_CLAUDE].env_var

# The env var that carries a plain API key for each app. For Codex this is the
# same variable as its rotation-free credential (an API key IS Codex's
# rotation-free credential); for Claude it is a distinct auth source that
# outranks everything else.
API_KEY_ENV = {APP_CLAUDE: "ANTHROPIC_API_KEY", APP_CODEX: "OPENAI_API_KEY"}

# Each app's refreshing login file, relative to the user's home.
_SUBSCRIPTION_CREDS = {
    APP_CLAUDE: Path(".claude") / ".credentials.json",
    APP_CODEX: Path(".codex") / "auth.json",
}

# Container-side sidecar basename (in the agent's home) where the stored
# rotation-free credential is bind-mounted read-only. VS Code resolves the
# spec's ``${localEnv:...}`` against its OWN process env — not the shell the
# user ran ``booley auth`` in — so a stored credential can only reach a
# "Reopen in Container" session through the container itself: the spec mounts
# the stored file here and ``incontainer_register`` applies it on every start
# (Claude: settings.json ``env``; Codex: ``auth.json``). Kept outside the
# app's state dir (``~/.claude``/``~/.codex``) to dodge state-volume nesting,
# same as the subscription-creds seed sidecars.
TOKEN_SEED_BASENAME = {
    APP_CLAUDE: ".booley-oauth-token-seed",
    APP_CODEX: ".booley-api-key-seed",
}


def credential_for_app(app: str) -> AppCredential | None:
    """The rotation-free credential definition for *app*, if it has one."""
    return CREDENTIALS.get(app)


def credential_for_env_var(name: str) -> AppCredential | None:
    """Reverse lookup: which app's credential is carried by env var *name*."""
    for cred in CREDENTIALS.values():
        if cred.env_var == name:
            return cred
    return None


def config_dir() -> Path:
    """Booley's per-user config dir, honouring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".config"
    return root / "booley"


def token_path(app: str = APP_CLAUDE) -> Path:
    """Absolute path of *app*'s stored credential."""
    cred = CREDENTIALS[app]
    return config_dir() / cred.filename


def read_stored_token(app: str = APP_CLAUDE) -> str | None:
    """*app*'s stored credential, or ``None`` when absent/unreadable/empty.

    Container-side, ``booley auth`` storage lives on the HOST — the generated
    spec bind-mounts it read-only at a home sidecar (see
    :data:`TOKEN_SEED_BASENAME`). Falling back to that sidecar makes
    :func:`resolve_token` / :func:`effective_credential` see the same
    credential in both launch contexts; on the host the sidecar path simply does not
    exist.
    """
    for path in (token_path(app), Path.home() / TOKEN_SEED_BASENAME[app]):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, KeyError):
            continue
        if raw.strip():
            return raw.strip()
    return None


def resolve_token(app: str = APP_CLAUDE) -> str | None:
    """*app*'s credential: env var wins, stored file fills the gap.

    Returns ``None`` rather than ``""`` when there is none, so callers can't
    accidentally forward an empty value and shadow the subscription credentials.
    """
    cred = CREDENTIALS.get(app)
    if cred is None:
        return None
    env = (os.environ.get(cred.env_var) or "").strip()
    return env or read_stored_token(app)


def subscription_creds_path(app: str = APP_CLAUDE) -> Path:
    """Absolute path of *app*'s refreshing login file (the subscription creds)."""
    return Path.home() / _SUBSCRIPTION_CREDS[app]


# Modes for :class:`EffectiveCredential`. ``api_key`` and ``oauth_token`` are
# rotation-free; ``subscription`` is the refreshing login file that a host-side
# refresh can rotate out from under a running container.
MODE_API_KEY = "api_key"
MODE_OAUTH_TOKEN = "oauth_token"
MODE_SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class EffectiveCredential:
    """The credential *app*'s agents actually authenticate (and bill) with."""

    app: str
    mode: str  # MODE_API_KEY | MODE_OAUTH_TOKEN | MODE_SUBSCRIPTION
    source: str  # where the winning value lives, human-readable
    rotation_free: bool
    # Credentials that are also present but LOSE to `source`, human-readable.
    # Callers should surface these: a silently outranked subscription login is
    # exactly how "detected subscription auth" ends up billing an API key.
    overridden: tuple[str, ...]


def _env_or_stored_source(app: str, cred: AppCredential, env_token: str) -> str:
    """Name whichever of the exported env var / stored file supplies the token."""
    if env_token:
        return f"{cred.env_var} (exported)"
    return f"the stored {cred.label} at {token_path(app)}"


def _api_key_policy_credential(
    app: str,
    cred: AppCredential,
    env_token: str,
    stored: str | None,
    api_key_is_distinct: bool,
) -> EffectiveCredential | None:
    """Resolve under ``[agent] auth = "api_key"``, where only the API key may bill.

    For Codex the API key doubles as the rotation-free credential, so the env
    var and the stored file still satisfy the policy. For Claude the key is
    distinct, and its caller's distinct-key branch was the only way to satisfy
    the policy — so reaching here means the key is absent, and ``None`` is the
    misconfiguration signal rather than a fallback.
    """
    if not api_key_is_distinct and (env_token or stored):
        return EffectiveCredential(
            app=app,
            mode=MODE_API_KEY,
            source=_env_or_stored_source(app, cred, env_token),
            rotation_free=True,
            overridden=(),
        )
    return None


def effective_credential(
    app: str = APP_CLAUDE, policy: str = "auto"
) -> EffectiveCredential | None:
    """Resolve which credential *app*'s agents would actually use, CLI-style.

    *policy* is the project's ``[agent] auth`` knob. ``auto`` models the bare
    CLI precedence below. ``subscription`` resolves as if the API-key env var
    were scrubbed (which is what Booley does to agent envs under that policy);
    for Codex the API key IS the rotation-free credential, so only the login
    file remains. ``api_key`` resolves the API key alone — ``None`` then means
    "declared api_key but no key present", which callers should treat as a
    misconfiguration rather than a fallback.

    The Claude CLI's precedence, verified against the 2.1.207 binary, is:
    ``ANTHROPIC_API_KEY`` first ("Unset ANTHROPIC_API_KEY to use your
    claude.ai account instead"), then ``CLAUDE_CODE_OAUTH_TOKEN`` ("is set in
    your environment and will override this login token at runtime"), then
    the refreshing login at ``~/.claude/.credentials.json``. Codex is
    modelled symmetrically; its API key doubles as the rotation-free
    credential, and the in-container registrar writes the stored key into
    ``auth.json`` anyway, so the two sources converge there.

    A stored rotation-free credential counts as an env-level source because
    every Booley-driven path injects it as one (env-file into containers,
    ``settings.json``/``auth.json`` via the registrar for VS Code sessions,
    ``options.env`` in ``_build_sdk_options`` for SDK ticket runs — SDK runs
    skip user settings, so the registrar's route does not reach them).

    Returns ``None`` when *app* has no usable credential at all.
    """
    cred = CREDENTIALS.get(app)
    if cred is None:
        return None

    env_token = (os.environ.get(cred.env_var) or "").strip()
    stored = None if env_token else read_stored_token(app)
    subs_path = subscription_creds_path(app)
    has_subscription = subs_path.is_file()
    subscription_desc = f"the subscription login at {subs_path}"

    api_key_var = API_KEY_ENV[app]
    api_key_is_distinct = api_key_var != cred.env_var  # claude: yes; codex: the token IS the key
    has_api_key = bool((os.environ.get(api_key_var) or "").strip())

    if policy != "subscription" and api_key_is_distinct and has_api_key:
        overridden = []
        if env_token:
            overridden.append(f"{cred.env_var} (exported)")
        elif stored:
            overridden.append(f"the stored {cred.label} at {token_path(app)}")
        if has_subscription:
            overridden.append(subscription_desc)
        return EffectiveCredential(
            app=app,
            mode=MODE_API_KEY,
            source=f"{api_key_var} (exported)",
            rotation_free=True,
            overridden=tuple(overridden),
        )

    if policy == "api_key":
        return _api_key_policy_credential(app, cred, env_token, stored, api_key_is_distinct)

    if (policy != "subscription" or api_key_is_distinct) and (env_token or stored):
        source = _env_or_stored_source(app, cred, env_token)
        mode = MODE_API_KEY if app == APP_CODEX else MODE_OAUTH_TOKEN
        overridden = (subscription_desc,) if has_subscription else ()
        return EffectiveCredential(
            app=app, mode=mode, source=source, rotation_free=True, overridden=overridden
        )

    if has_subscription:
        return EffectiveCredential(
            app=app,
            mode=MODE_SUBSCRIPTION,
            source=str(subs_path),
            rotation_free=False,
            overridden=(),
        )
    return None


def resolve_oauth_token() -> str | None:
    """Claude's credential. Kept as a named helper: it is the one the generated
    devcontainer spec references by name."""
    return resolve_token(APP_CLAUDE)


def store_token(token: str, app: str = APP_CLAUDE) -> Path:
    """Persist *token* for *app* with mode 0600. Returns the path.

    Whitespace is rejected outright rather than stripped mid-string: the value is
    written verbatim into a Docker ``--env-file``, where an embedded newline would
    silently corrupt every following variable.
    """
    token = token.strip()
    if not token:
        raise ValueError("refusing to store an empty credential")
    if any(ch.isspace() for ch in token):
        raise ValueError("credential contains whitespace — it would corrupt a docker --env-file")

    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(stat.S_IRWXU)  # 0700

    path = token_path(app)
    # Open with 0600 BEFORE any content lands, so the secret is never briefly
    # world-readable (a plain write + chmod has exactly that window).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 even if the file pre-existed
    return path


def clear_stored_token(app: str = APP_CLAUDE) -> bool:
    """Delete *app*'s stored credential. True if one was there."""
    try:
        token_path(app).unlink()
    except (FileNotFoundError, OSError):
        return False
    return True


def stored_token_mode(app: str = APP_CLAUDE) -> int | None:
    """Permission bits of *app*'s stored credential, or ``None`` when absent."""
    try:
        return stat.S_IMODE(token_path(app).stat().st_mode)
    except OSError:
        return None


def stored_token_is_private(app: str = APP_CLAUDE) -> bool:
    """Whether *app*'s stored credential is readable only by its owner."""
    if os.name == "nt":
        # Windows reports synthetic POSIX mode bits (commonly 0666) that do
        # not describe the file's ACL. Treating those bits as an ACL made every
        # freshly stored credential fail Doctor immediately on Windows.
        return token_path(app).is_file()
    mode = stored_token_mode(app)
    return mode is not None and not mode & (stat.S_IRWXG | stat.S_IRWXO)
