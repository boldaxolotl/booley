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
``.devcontainer/devcontainer.json``. A long-lived credential inside the Project Root is one ``git add -f`` away from being published, and the
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

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from booley.config.gateway import (
    GATEWAY_AUTH,
    GatewayConfigError,
    gateway_settings_from_env,
)
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir

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


def token_path(app: str = APP_CLAUDE) -> Path:
    """Absolute path of *app*'s stored credential."""
    cred = CREDENTIALS[app]
    return config_dir() / cred.filename


def read_stored_token(app: str = APP_CLAUDE, *, home: Path | None = None) -> str | None:
    """*app*'s stored credential, or ``None`` when absent/unreadable/empty.

    Container-side, ``booley auth`` storage lives on the HOST — the generated
    spec bind-mounts it read-only at a home sidecar (see
    :data:`TOKEN_SEED_BASENAME`). Falling back to that sidecar makes
    :func:`resolve_token` / :func:`effective_credential` see the same
    credential in both launch contexts; on the host the sidecar path simply does not
    exist.
    """
    paths = (
        (token_path(app), Path.home() / TOKEN_SEED_BASENAME[app])
        if home is None
        else (home / TOKEN_SEED_BASENAME[app],)
    )
    for path in paths:
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


def subscription_creds_path(app: str = APP_CLAUDE, *, home: Path | None = None) -> Path:
    """Absolute path of *app*'s refreshing login file (the subscription creds)."""
    if app == APP_CODEX and home is None and (codex_home := os.environ.get("CODEX_HOME")):
        return Path(codex_home).resolve() / "auth.json"
    return (home or Path.home()) / _SUBSCRIPTION_CREDS[app]


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


def _gateway_credential(app: str) -> EffectiveCredential | None:
    """The gateway as *app*'s credential; ``None`` if incomplete or *app* is not Codex."""
    if app != APP_CODEX:
        return None
    try:
        settings = gateway_settings_from_env()
    except GatewayConfigError:
        return None
    return EffectiveCredential(
        app=app,
        mode=GATEWAY_AUTH,
        source=f"the gateway at {settings.url} ({settings.token_source})",
        rotation_free=True,
        overridden=(),
    )


def effective_credential(
    app: str = APP_CLAUDE, policy: str = "auto", *, home: Path | None = None
) -> EffectiveCredential | None:
    """Resolve which credential *app*'s agents would actually use under *policy*.

    ``gateway`` is environment-selected and has no native credential; every
    other policy resolves the native credential, CLI-style.
    """
    if policy == GATEWAY_AUTH:
        return _gateway_credential(app)
    return _native_credential(app, policy, home=home)


def _native_credential(
    app: str, policy: str, *, home: Path | None = None
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
    stored = None if env_token else read_stored_token(app, home=home)
    subs_path = subscription_creds_path(app, home=home)
    has_subscription = subscription_available(app, policy, subs_path)
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


class CredentialStoreError(RuntimeError):
    """Credential storage is unsafe or unavailable; no secret belongs in the error."""


def _token_store() -> PrivateStore:
    directory = config_dir()
    return PrivateStore(directory, directory.parent, "credential store", CredentialStoreError)


def _validate_token_file(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise CredentialStoreError(f"credential must be a regular file: {path}")
    if os.name != "nt" and info.st_uid != os.getuid():
        raise CredentialStoreError(f"credential must be owned by current user: {path}")


def preflight_store_token(app: str = APP_CLAUDE) -> None:
    """Inspect existing credential paths without creating or changing them."""
    store = _token_store()
    store.validate_existing_directory()
    path = token_path(app)
    if path.is_symlink():
        raise CredentialStoreError(f"credential must not be a symlink: {path}")
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    _validate_token_file(path, info)


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

    preflight_store_token(app)
    _token_store().ensure_directory()
    path = token_path(app)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n")
    path.chmod(0o600)
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


# ``ANTHROPIC_AUTH_TOKEN`` is a Claude-CLI bearer that outranks the login file.
# It is tracked so policies that disallow it scrub it from agent environments,
# but it is scrub-only: Booley never forwards it into a Sandbox spec.
SCRUB_ONLY_ENV_KEYS = frozenset({"ANTHROPIC_AUTH_TOKEN"})
CREDENTIAL_ENV_KEYS = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"} | SCRUB_ONLY_ENV_KEYS
)
CREDENTIAL_SCHEMA = "1"


def subscription_available(app: str, policy: str, path: Path) -> bool:
    """Inspect explicit-policy login material without exposing its contents."""
    if policy == "auto":
        return path.is_file()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    if not isinstance(data, dict):
        return False
    if app == APP_CODEX:
        tokens = data.get("tokens")
        key = data.get("OPENAI_API_KEY")
        return (
            isinstance(tokens, dict)
            and any(
                isinstance(tokens.get(name), str) and tokens[name].strip()
                for name in ("access_token", "refresh_token")
            )
            and key in (None, "")
        )
    oauth = data.get("claudeAiOauth")
    return isinstance(oauth, dict) and any(
        isinstance(oauth.get(name), str) and oauth[name].strip()
        for name in ("accessToken", "refreshToken")
    )


def allowed_credential_keys(app: str, policy: str) -> frozenset[str]:
    """Deterministic transport permissions, independent of shell exports."""
    if policy not in {"auto", "subscription", "api_key", GATEWAY_AUTH}:
        raise ValueError("invalid credential policy; refresh the Sandbox")
    if app not in CREDENTIALS or policy == GATEWAY_AUTH:
        return frozenset()
    if policy == "api_key":
        return frozenset({API_KEY_ENV[app]})
    if policy == "subscription":
        return frozenset({ENV_VAR}) if app == APP_CLAUDE else frozenset()
    allowed = {API_KEY_ENV[app], CREDENTIALS[app].env_var}
    if app == APP_CLAUDE:
        # ``auto`` leaves the CLI's own precedence untouched, bearer included.
        allowed |= SCRUB_ONLY_ENV_KEYS
    return frozenset(allowed)


@dataclass(frozen=True)
class CredentialPlan:
    """One provider's billing selection and permitted credential transports."""

    app: str
    policy: str
    selected: EffectiveCredential | None
    allowed_env: frozenset[str]
    values: tuple[tuple[str, str], ...] = field(repr=False)
    subscription_seed: Path | None = None
    token_seed: Path | None = None
    config_seed: Path | None = None

    @property
    def forbidden_env(self) -> frozenset[str]:
        return CREDENTIAL_ENV_KEYS - self.allowed_env

    def child_environment(self, inherited: dict[str, str]) -> dict[str, str]:
        """Render a child env without mutating the launching process."""
        return {
            **{k: v for k, v in inherited.items() if k not in self.forbidden_env},
            **dict(self.values),
        }

    def require_credential(self) -> None:
        """Pinned modes must never silently fall through to another source."""
        if self.policy == GATEWAY_AUTH and self.selected is None:
            if self.app != APP_CODEX:
                raise ValueError(
                    f"auth '{GATEWAY_AUTH}' routes Codex only; set [agent] provider = 'codex'"
                )
            gateway_settings_from_env()  # raises GatewayConfigError naming the variable
        if self.policy != "auto" and self.selected is None:
            raise ValueError(
                f"{self.app}: no usable credential under auth = '{self.policy}'; provide that credential and refresh the Sandbox"
            )


def inspect_config_seed(path: Path, policy: str) -> None:
    """Refuse ambiguous auth-bearing Claude config; never edit host config."""
    if policy == "auto" or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(
            "cannot inspect Claude config seed; remove the seed or use an inspected config"
        ) from exc
    if not isinstance(data, dict) or any(
        data.get(key) for key in ("primaryApiKey", "apiKeyHelper", "customApiKeyResponses")
    ):
        raise ValueError(
            "Claude config seed contains ambiguous credential state; use a config without credential fields"
        )


def credential_plan(
    app: str = APP_CLAUDE, policy: str = "auto", *, home: Path | None = None
) -> CredentialPlan:
    """Resolve once, keeping secrets out of repr and serialized metadata."""
    allowed = allowed_credential_keys(app, policy)
    if app not in CREDENTIALS:
        return CredentialPlan(app, policy, None, allowed, ())
    values = {}
    for key in sorted(allowed - SCRUB_ONLY_ENV_KEYS):
        value = (os.environ.get(key) or "").strip()
        if not value and key == CREDENTIALS[app].env_var:
            value = read_stored_token(app, home=home) or ""
        if value:
            if any(character.isspace() for character in value):
                raise ValueError("credential contains whitespace; cannot forward it safely")
            values[key] = value
    login = subscription_creds_path(app, home=home)
    subscription = (
        login
        if policy not in {"api_key", GATEWAY_AUTH} and subscription_available(app, policy, login)
        else None
    )
    token = token_path(app) if home is None else home / TOKEN_SEED_BASENAME[app]
    token_seed = (
        token
        if CREDENTIALS[app].env_var in allowed
        and token.is_file()
        and read_stored_token(app, home=home)
        else None
    )
    config = (home or Path.home()) / ".claude.json" if app == APP_CLAUDE else None
    if config is not None:
        inspect_config_seed(config, policy)
        config = config if config.is_file() else None
    return CredentialPlan(
        app,
        policy,
        effective_credential(app, policy, home=home),
        allowed,
        tuple(sorted(values.items())),
        subscription,
        token_seed,
        config,
    )


def link_codex_auth(source: Path, destination: Path, policy: str = "auto") -> None:
    """Make a Booley-owned private auth entry a link to the real login.

    A link, not a copy: Codex rotates ``auth.json`` in place, so a copy would
    strand the rotated tokens in scratch and leave a credential behind. When
    the policy excludes the login, or it is missing, no entry remains.
    """
    allowed_credential_keys(APP_CODEX, policy)
    allowed = policy == "auto" or (
        policy == "subscription" and subscription_available(APP_CODEX, policy, source)
    )
    if not (source.is_file() and allowed):
        destination.unlink(missing_ok=True)
        return
    # Resolve so a link never points at another (removable) link.
    target = source.resolve()
    if destination.is_symlink() and destination.readlink() == target:
        return
    pending = destination.parent / f".{destination.name}.{os.getpid()}.{os.urandom(4).hex()}"
    try:
        pending.symlink_to(target)
        # Atomic: an interrupted relink keeps the previous entry.
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def credential_warnings(plan: CredentialPlan) -> tuple[str, ...]:
    """Nonsecret source-selection caveats for status presentation."""
    if plan.app != APP_CLAUDE or plan.config_seed is None:
        return ()
    try:
        data = json.loads(plan.config_seed.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return (
            "Claude config could not be inspected; interactive credential approval is unknown",
        )
    if isinstance(data, dict) and data.get("customApiKeyResponses"):
        return (
            "Claude config records interactive API-key approval/rejection state; source selection does not attest interactive approval",
        )
    return ()
