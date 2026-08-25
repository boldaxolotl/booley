"""Dual-backend architecture: BackendConfig, SandboxConfig, model loading."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.core.config_paths import resolve_toml
from booley.runtime.job_slots import SlotCaps

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# The provider used when nothing else pins one. Claude — never codex — so a
# missing/dropped config degrades to Booley's actual backend instead of an
# unauthenticated Codex call that 401s. Mirrors _DEFAULT_TIER_MODELS below,
# which already sources Claude model strings.
_DEFAULT_PROVIDER = "claude"

_VALID_PROVIDERS = ("codex", "claude")

# [agent] auth: which credential the agents must bill.
#   auto         — today's behavior: the agent CLI resolves per its own
#                  precedence (API key > rotation-free token > login file).
#   subscription — bill the subscription: Booley scrubs the API-key env vars
#                  (ANTHROPIC_API_KEY / OPENAI_API_KEY) from every agent
#                  environment it controls, which is exactly the remedy the
#                  Claude CLI itself prescribes ("Unset ANTHROPIC_API_KEY to
#                  use your claude.ai account instead").
#   api_key      — bill the API key. The key already outranks every other
#                  credential, so nothing is scrubbed; the backend health
#                  check fails loud when the key is absent instead of letting
#                  agents silently fall back to (and bill) the subscription.
_VALID_AUTH_MODES = ("auto", "subscription", "api_key")
_DEFAULT_AUTH = "auto"

# The three capability tiers every provider maps to concrete models.
MODEL_TIERS = ("heavy", "standard", "light")
_MODEL_TIERS = MODEL_TIERS

# [models.roles] vocabulary: the agents whose model may be pinned by name.
# Two kinds of role live here and they resolve through different call paths:
#
#   * harness steps (``developer``, ``recovery``) — keys of config.STEP_TIERS,
#     resolved in-process when load_models_config() syncs MODEL_MAP.
#   * specialists (``reviewer``, ``mutation_tester``, …) — each runs as its own
#     `python -m booley.dev_support.<name>` subprocess and re-reads booley.toml, so
#     the role key must equal the specialist's ``Specialist.name``.
#
# The list is explicit rather than derived from the MCP endpoint registry: importing
# the registry here would drag the whole endpoint packages into the config layer,
# which bare specialist subprocesses import at startup. A registry cross-check
# test (tests/harness/test_role_models.py) fails if a new specialist is added
# without extending this set, so it cannot drift silently.
KNOWN_ROLES = frozenset(
    {
        "developer",
        "recovery",
        "triage_report",
        "reviewer",
        "mutation_tester",
        "coverage_analyst",
        "tb_coder",
    }
)
_KNOWN_ROLES = KNOWN_ROLES


class BackendConfigError(RuntimeError):
    """Agent provider could not be resolved and must not be guessed.

    Raised instead of silently falling back to a backend the run never asked
    for (the old codex default surfaced as a mystery 401 from an
    unauthenticated Codex call).
    """


def _inside_container() -> bool:
    """Detect if we're running inside a Docker container."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


_PROVIDER_TIER_MODELS: dict[str, dict[str, str]] = {
    "claude": {
        "heavy": "claude-fable-5",
        "standard": "claude-opus-4-8",
        # Light tier is Sonnet, not Haiku: Booley never routes work to Haiku.
        "light": "claude-sonnet-5",
    },
    "codex": {
        # GPT-5.6's Sol/Terra/Luna are durable capability tiers that map onto
        # Booley's three directly — unlike gpt-5.5, which had no distinct
        # standard tier and was used for both heavy and standard.
        "heavy": "gpt-5.6-sol",
        "standard": "gpt-5.6-terra",
        "light": "gpt-5.6-luna",
    },
}

_PROVIDER_TIER_EFFORT: dict[str, dict[str, str]] = {
    "codex": {
        "heavy": "high",
        "standard": "medium",
        "light": "medium",
    },
    # NOTE: Claude is intentionally absent — the Agent SDK exposes no clean
    # per-call reasoning-effort knob, so effort_for_tier() returns None for
    # Claude tiers (documented no-op). Add a mapping here if/when the SDK
    # surfaces an extended-thinking budget hook.
}

_DEFAULT_TIER_MODELS: dict[str, str] = dict(_PROVIDER_TIER_MODELS["claude"])


SANDBOX_IMAGE = "booley-sandbox"


@dataclass
class SandboxConfig:
    """Sandbox configuration parsed from booley.toml [sandbox].

    ADR 0028: Booley is container-only — everything runs inside the one
    Session Runtime (devcontainer), so there are no per-Flow containers to
    size or route. ``memory`` is the single container memory limit fed into
    the generated devcontainer; the empty default means "no explicit limit"
    (the pre-ADR-0028 devcontainer set none, so existing installs see no
    change until they opt in via ``[sandbox] memory = "8g"``).
    """

    image: str = SANDBOX_IMAGE
    memory: str = ""
    # Opt-in: bind the user's HOST agent skills (``~/.claude/skills`` +
    # ``~/.agents/skills``) into the sandbox read-only, so global personal
    # skills are usable inside the container alongside Booley's built-ins.
    # Off by default — enabling it exposes host skill content to the agent.
    mount_host_skills: bool = False


@dataclass
class BackendConfig:
    """Holds the single active backend and tier-to-model mapping.

    Single source of truth for "which backend + which model" a given
    step_name should use. Both the developer and every specialist run on
    the one configured provider. Testable via set_backend_config().
    """

    active_backend: Any = None
    tier_models: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_TIER_MODELS))
    # [models.roles]: role name -> tier name or literal model id. Empty = every
    # role takes its step/floor tier, which is the pre-knob behavior.
    role_models: dict[str, str] = field(default_factory=dict)
    auth: str = _DEFAULT_AUTH
    provider: str = _DEFAULT_PROVIDER
    # NOTE: the default provider is _DEFAULT_PROVIDER, applied in
    # get_backend_config(), load_models_config(), and _parse_provider().
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    jobs: SlotCaps = field(default_factory=SlotCaps)

    def model_for_tier(self, tier: str) -> str:
        return self.tier_models.get(tier, self.tier_models["standard"])

    def model_for_role(self, role: str, tier: str) -> str:
        """Model for *role*, falling back to *tier* when the role isn't pinned.

        A ``[models.roles]`` value may name a tier (``"light"``) or a literal
        model id (``"claude-sonnet-5"``). Tier names resolve through
        ``tier_models``, so a role pinned to a tier still tracks any
        ``[models]`` override of that tier.

        An explicit pin deliberately overrides a specialist's ``min_model``
        floor: the floor guards Booley's own tier defaults, not a choice the
        user wrote down.
        """
        pinned = self.role_models.get(role)
        if pinned is None:
            return self.model_for_tier(tier)
        return self.tier_models.get(pinned, pinned)

    def effort_for_tier(self, tier: str) -> str | None:
        """Resolve reasoning effort for a tier. None = use provider default."""
        provider_effort = _PROVIDER_TIER_EFFORT.get(self.provider, {})
        return provider_effort.get(tier)

    def backend_for_tier(self, tier: str) -> Any:
        """Return the backend for the given tier — always the single active one."""
        return self.active_backend


_backend_config: BackendConfig | None = None


def _make_backend(provider: str, auth_mode: str) -> Any:
    """Instantiate the native backend for a provider name."""
    if provider == "claude":
        from booley.runtime.agent_backend import ClaudeSDKBackend

        return ClaudeSDKBackend(auth_mode=auth_mode)
    from booley.runtime.agent_backend import CodexBackend

    return CodexBackend(auth_mode=auth_mode)


def _resolve_tier_models(provider: str, overrides: dict[str, str] | None) -> dict[str, str]:
    """The provider's tier defaults with any declared ``[models]`` tiers applied.

    *overrides* is sparse — only the tiers the project actually declared — so
    the unspecified ones follow the resolved provider. Merging here rather than
    at parse time matters when the provider is settled *after* the toml is read
    (a ``BOOLEY_PRIMARY_PROVIDER`` hand-off beats ``[agent] provider``);
    filling gaps at parse time would splice Claude defaults into a Codex run.
    """
    tiers = dict(_PROVIDER_TIER_MODELS[provider])
    tiers.update(overrides or {})
    return tiers


def _build_backend_config(
    provider: str,
    auth: str,
    tier_overrides: dict[str, str] | None = None,
    role_models: dict[str, str] | None = None,
) -> BackendConfig:
    """Assemble a BackendConfig for a resolved (provider, auth) pair."""
    return BackendConfig(
        active_backend=_make_backend(provider, auth),
        tier_models=_resolve_tier_models(provider, tier_overrides),
        role_models=dict(role_models or {}),
        provider=provider,
        auth=auth,
    )


def _lazy_backend_config() -> BackendConfig:
    """Resolve a BackendConfig when nothing has been explicitly configured.

    Resolution order, most-authoritative first:

    1. ``BOOLEY_PRIMARY_PROVIDER`` — the developer's hand-off to nested
       specialists inside a container.
    2. The project's ``booley.toml`` reached via ``BOOLEY_PROJECT_DIR``. This
       is what lets a specialist *subprocess* honor ``provider`` even when the
       env hand-off was dropped (a stale image, a missed propagation) — the
       toml is mounted in the container, so it's a reliable second source.
    3. ``BOOLEY_AGENT_APP`` — the devcontainer always exports which agent app
       runs the session (``claude``/``codex``/``none``; see
       ``devcontainer.build_devcontainer_spec``). Its vocabulary is exactly
       the provider vocabulary, so when a bare specialist is launched from a
       terminal inside the container — with no developer to hand off
       ``BOOLEY_PRIMARY_PROVIDER`` and no ``[agent] provider`` in booley.toml —
       the app signal reliably names the backend. Falling back to it turns a
       hard ``BackendConfigError`` into a working standalone invocation.
    4. Fail loud inside a container only when even the app signal is absent
       (``none``/unset): tell the user exactly how to pin the provider. On the
       host (tests, ad-hoc MCP endpoint runs) fall back to ``_DEFAULT_PROVIDER`` —
       Claude, never codex, so a missing config can't 401.
    """
    import os

    # Read the project toml once, up front: whichever branch settles the
    # provider, the project's [models] / [models.roles] still apply. Without
    # this a specialist subprocess — which never calls load_models_config —
    # would silently ignore every model the project pinned.
    project = _project_config_from_env()
    tiers = project.tier_models if project else None
    roles = project.role_models if project else None

    env_provider = os.environ.get("BOOLEY_PRIMARY_PROVIDER")
    env_auth = os.environ.get("BOOLEY_PRIMARY_AUTH")
    auth = (
        _env_auth()
        if env_auth is not None
        else (project.auth if project else None) or _DEFAULT_AUTH
    )
    if env_provider is not None:
        if env_provider not in _VALID_PROVIDERS:
            logger.warning(
                "Unknown BOOLEY_PRIMARY_PROVIDER %r; using %s", env_provider, _DEFAULT_PROVIDER
            )
            env_provider = _DEFAULT_PROVIDER
        return _build_backend_config(env_provider, auth, tiers, roles)

    if project is not None and project.provider is not None:
        logger.info("Resolved provider=%s from project booley.toml", project.provider)
        return _build_backend_config(project.provider, project.auth or _DEFAULT_AUTH, tiers, roles)

    app_provider = os.environ.get("BOOLEY_AGENT_APP")
    if app_provider in _VALID_PROVIDERS:
        logger.info(
            "Resolved provider=%s from BOOLEY_AGENT_APP (no developer "
            "hand-off — standalone specialist invocation)",
            app_provider,
        )
        return _build_backend_config(app_provider, auth, tiers, roles)

    if _inside_container():
        raise BackendConfigError(
            "Agent provider unresolved inside container: BOOLEY_PRIMARY_PROVIDER "
            "is unset, no booley.toml with an [agent] provider was found at "
            "BOOLEY_PROJECT_DIR, and BOOLEY_AGENT_APP names no provider. "
            "If you are running a specialist directly (e.g. "
            "`python -m booley.dev_support.reviewer ...`), pin the backend with one "
            "of:\n"
            "  * export BOOLEY_PRIMARY_PROVIDER=claude   # or codex\n"
            '  * add an [agent] provider = "claude" block to booley.toml\n'
            "In a developer run this instead means the propagation broke — "
            "rebuild the sandbox image if it predates the provider hand-off."
        )

    return _build_backend_config(_DEFAULT_PROVIDER, auth, tiers, roles)


def get_backend_config() -> BackendConfig:
    """Return the current BackendConfig (lazily initialized when unset).

    See :func:`_lazy_backend_config` for how the provider is resolved when no
    config has been installed via :func:`load_models_config` /
    :func:`set_backend_config`.
    """
    global _backend_config
    if _backend_config is None:
        _backend_config = _lazy_backend_config()
    return _backend_config


def set_backend_config(cfg: BackendConfig) -> None:
    """Replace the global BackendConfig (for testing)."""
    global _backend_config
    _backend_config = cfg


@dataclass(frozen=True)
class _ProjectAgentConfig:
    """What the project's booley.toml says about agents and their models.

    ``provider``/``auth`` are ``None`` when undeclared — the caller decides
    whether an env hand-off or a default fills them in. ``tier_models`` is
    sparse (only the tiers ``[models]`` actually declared) so it can be merged
    against whichever provider ultimately wins.
    """

    provider: str | None = None
    auth: str | None = None
    tier_models: dict[str, str] | None = None
    role_models: dict[str, str] = field(default_factory=dict)


def _project_config_from_env() -> _ProjectAgentConfig | None:
    """Read the project's agent/model config from booley.toml via env.

    Reads ``BOOLEY_PROJECT_DIR`` and looks for ``booley.toml`` under it. Handles
    both layouts the harness produces:

      * container: ``BOOLEY_PROJECT_DIR=/booley-project`` with the toml mounted
        flat at ``/booley-project/booley.toml``.
      * host:      ``BOOLEY_PROJECT_DIR=<repo>/.booley_project`` — the toml sits
        directly inside, so the same flat probe finds it.

    This is the path a *specialist subprocess* takes: it never calls
    ``load_models_config``, so without this it would see none of the project's
    ``[models]`` pins. Returns ``None`` when the dir/toml is absent or
    unreadable. An *invalid* provider/auth/role still raises (fail loud).
    """
    import os

    project_dir = os.environ.get("BOOLEY_PROJECT_DIR")
    if not project_dir:
        return None

    base = Path(project_dir)
    for toml_path in (base / "booley.toml", base / ".booley_project" / "booley.toml"):
        if not toml_path.is_file():
            continue
        try:
            import tomllib

            with toml_path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, ImportError, ValueError) as e:
            logger.warning("Failed to read agent config from %s: %s", toml_path, e)
            continue
        agent = data.get("agent", {})
        if not isinstance(agent, dict):
            agent = {}
        models = data.get("models", {})
        if not isinstance(models, dict):
            models = {}
        return _ProjectAgentConfig(
            provider=_parse_provider(agent),
            auth=_parse_auth(agent),
            tier_models=_parse_tier_models(models),
            role_models=_parse_role_models(models),
        )
    return None


def _parse_sandbox_config(data: dict) -> SandboxConfig:
    """Parse [sandbox] from booley.toml (single container memory limit).

    Legacy pre-ADR-0028 memory tier knobs are warned about and ignored; the
    Session Runtime has one memory limit.
    """
    section = data.get("sandbox", {})
    if "mode" in section:
        logger.warning(
            "[sandbox].mode is retired: the Session Runtime is always Docker; delete it"
        )
    image = section.get("image", SANDBOX_IMAGE)
    if not isinstance(image, str) or not image.strip():
        logger.warning("Invalid sandbox image %r; falling back to %r", image, SANDBOX_IMAGE)
        image = SANDBOX_IMAGE

    memory = ""
    mem_val = section.get("memory", "")
    if isinstance(mem_val, str):
        memory = mem_val.strip()
    elif isinstance(mem_val, dict):
        # Legacy tier table ({default/agent/sim/synth}): per-Flow containers
        # are gone (ADR 0028), so tiers have nothing to size. Keep a usable
        # limit when one is derivable, but tell the user to migrate.
        logger.warning(
            "[sandbox] memory tier table is retired (ADR 0028: container-only, "
            'one memory limit); use a single string, e.g. memory = "8g"',
        )
        legacy_default = mem_val.get("default", "")
        if isinstance(legacy_default, str):
            memory = legacy_default.strip()
    elif mem_val:
        logger.warning("Invalid [sandbox] memory %r; ignoring", mem_val)
    if "memory_tiers" in section:
        logger.warning(
            "[sandbox] memory_tiers is retired (ADR 0028: container-only, one "
            'memory limit); use memory = "8g" — ignoring',
        )

    mount_host_skills = bool(section.get("mount_host_skills", False))

    return SandboxConfig(image=image, memory=memory, mount_host_skills=mount_host_skills)


def _parse_jobs_config(data: dict) -> SlotCaps:
    """Parse [jobs] concurrency caps (ADR 0028 Decision 5).

    Thin alias for ``job_slots.parse_caps`` — the logic lives beside the slot
    store so MCP endpoint subprocesses can resolve caps without importing the harness.
    """
    from booley.runtime.job_slots import parse_caps

    return parse_caps(data)


def _resolve_booley_toml(project_data: Path) -> Path:
    """Resolve booley.toml in *project_data*, warning on legacy pipeline.toml."""
    path = resolve_toml(project_data)
    if path.name == "pipeline.toml" and path.exists():
        logger.warning("Using legacy pipeline.toml — rename to booley.toml")
    return path


def load_models_config(project_root: Path) -> None:
    """Load [agent] + [sandbox] from booley.toml and configure the backend + MODEL_MAP.

    The single provider is selected via ``[agent] provider`` (default:
    ``_DEFAULT_PROVIDER``). The developer and every specialist run on it.
    Auth mode, explicit model tiers, and sandbox config are also read from the
    TOML. An explicitly *invalid* provider raises ``BackendConfigError`` —
    we never silently run a backend the project didn't ask for.
    """
    global _backend_config

    from .settings import MODEL_MAP, STEP_TIERS

    (auth, tier_overrides, provider, role_models, sandbox_cfg, jobs_cfg) = _load_toml_agent_config(
        project_root
    )
    if provider is None:
        provider = _DEFAULT_PROVIDER

    _backend_config = BackendConfig(
        active_backend=_make_backend(provider, auth),
        tier_models=_resolve_tier_models(provider, tier_overrides),
        role_models=role_models,
        auth=auth,
        provider=provider,
        sandbox=sandbox_cfg,
        jobs=jobs_cfg,
    )

    # Harness steps read MODEL_MAP, so per-role pins have to land in it too —
    # otherwise [models.roles] developer = … would be ignored by the one agent
    # users are most likely to pin.
    for step_key, tier in STEP_TIERS.items():
        MODEL_MAP[step_key] = _backend_config.model_for_role(step_key, tier)


def _load_toml_agent_config(
    project_root: Path,
) -> tuple[str, dict[str, str] | None, str | None, dict[str, str], SandboxConfig, SlotCaps]:
    """Parse booley.toml [agent]/[sandbox]/[models]/[jobs].

    Returns (auth, tier_overrides, provider, role_models, sandbox, jobs).
    ``provider`` is ``None`` when the toml is missing/unparseable or declares
    no provider — the caller applies ``_DEFAULT_PROVIDER``. An *invalid*
    provider, auth, or role raises (fail loud).
    """
    bp = project_root / ".booley_project"
    project_data = bp if bp.is_dir() else project_root / ".booley" / "project"
    toml_path = _resolve_booley_toml(project_data)

    auth = _DEFAULT_AUTH
    tier_overrides: dict[str, str] | None = None
    provider: str | None = None
    role_models: dict[str, str] = {}
    sandbox_cfg = SandboxConfig()
    jobs_cfg = SlotCaps()

    if not toml_path.exists():
        logger.debug("No booley.toml at %s; using defaults", toml_path)
        return (auth, tier_overrides, provider, role_models, sandbox_cfg, jobs_cfg)

    try:
        import tomllib
    except ImportError:
        logger.warning("tomllib unavailable; using default model config")
        return (auth, tier_overrides, provider, role_models, sandbox_cfg, jobs_cfg)

    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)

        sandbox_cfg = _parse_sandbox_config(data)
        sandbox_section = data.get("sandbox", {})
        has_explicit_image = isinstance(sandbox_section, dict) and bool(
            str(sandbox_section.get("image", "")).strip()
        )
        if not has_explicit_image and (project_data / "docker" / "Dockerfile").is_file():
            from booley.runtime.project_image import project_image_name

            sandbox_cfg.image = project_image_name(project_root)
        jobs_cfg = _parse_jobs_config(data)
        (auth, tier_overrides, provider, role_models) = _parse_agent_and_models(data, auth)
        logger.debug(
            "Loaded booley.toml [agent]: %s (provider), %d role model pin(s)",
            provider,
            len(role_models),
        )
    except (OSError, KeyError, TypeError, ValueError) as e:
        logger.warning("Failed to parse booley.toml: %s (using defaults)", e)

    return (auth, tier_overrides, provider, role_models, sandbox_cfg, jobs_cfg)


def parse_provider(agent_section: Mapping[str, Any]) -> str | None:
    """Resolve the declared agent provider, or ``None`` when [agent] omits it.

    ``provider`` is the sole spelling. An explicitly invalid value raises
    ``BackendConfigError`` — a typo must not silently run a backend the project
    never chose.
    """
    raw = agent_section.get("provider")
    if raw is None:
        return None
    if raw not in _VALID_PROVIDERS:
        raise BackendConfigError(
            f"booley.toml [agent] provider={raw!r} is invalid; use 'codex' or 'claude'."
        )
    return raw


_parse_provider = parse_provider


def parse_auth(agent_section: Mapping[str, Any]) -> str | None:
    """Resolve the declared auth mode, or ``None`` when [agent] omits it.

    ``auth`` is the sole spelling. An explicitly invalid value raises
    ``BackendConfigError`` — a typo
    like ``auth = "subscripton"`` must not silently bill a credential the
    project never chose.
    """
    raw = agent_section.get("auth")
    if raw is None:
        return None
    if raw not in _VALID_AUTH_MODES:
        raise BackendConfigError(
            f"booley.toml [agent] auth={raw!r} is invalid; "
            f"use one of {', '.join(repr(m) for m in _VALID_AUTH_MODES)}."
        )
    return raw


_parse_auth = parse_auth


def _env_auth() -> str:
    """The auth mode from ``BOOLEY_PRIMARY_AUTH``, warning-and-defaulting on junk.

    The env var is a harness hand-off, not user config, so an unknown value is
    degraded to the default with a warning rather than raised (mirrors how
    ``_lazy_backend_config`` treats an unknown ``BOOLEY_PRIMARY_PROVIDER``).
    """
    import os

    raw = os.environ.get("BOOLEY_PRIMARY_AUTH", _DEFAULT_AUTH)
    if raw not in _VALID_AUTH_MODES:
        logger.warning("Unknown BOOLEY_PRIMARY_AUTH %r; using %s", raw, _DEFAULT_AUTH)
        return _DEFAULT_AUTH
    return raw


def resolve_auth_policy() -> str:
    """The effective ``[agent] auth`` policy, resolved without side effects.

    For callers that need only the auth policy (sandbox env forwarding, doctor
    reporting) and must never trip ``_lazy_backend_config``'s fail-loud
    provider resolution. Order: ``BOOLEY_PRIMARY_AUTH`` (the developer's
    hand-off into containers) beats the project's booley.toml; absent both,
    ``auto``. Never raises: an invalid toml value degrades to ``auto`` with a
    warning here — the loud ``BackendConfigError`` belongs to the config-load
    path, not to a reporting/forwarding helper.
    """
    import os

    if os.environ.get("BOOLEY_PRIMARY_AUTH"):
        return _env_auth()

    project_dir = os.environ.get("BOOLEY_PROJECT_DIR")
    if not project_dir:
        return _DEFAULT_AUTH
    base = Path(project_dir)
    for toml_path in (base / "booley.toml", base / ".booley_project" / "booley.toml"):
        if not toml_path.is_file():
            continue
        try:
            import tomllib

            with toml_path.open("rb") as f:
                data = tomllib.load(f)
            agent = data.get("agent", {})
            if isinstance(agent, dict):
                return _parse_auth(agent) or _DEFAULT_AUTH
        except (OSError, ValueError, BackendConfigError) as e:
            logger.warning("Failed to read [agent] auth from %s: %s", toml_path, e)
        break
    return _DEFAULT_AUTH


def _parse_tier_models(models_section: dict) -> dict[str, str] | None:
    """Parse the tier keys of ``[models]``, or ``None`` when none are declared.

    Sparse by design: an undeclared tier must follow the resolved provider's
    default, so it is left absent rather than pre-filled here. Non-tier keys
    (``roles``, and anything else) are handled by ``_parse_role_models`` and
    doctor's table validation, not here.
    """
    tiers = {
        tier: models_section[tier]
        for tier in _MODEL_TIERS
        if isinstance(models_section.get(tier), str) and models_section[tier].strip()
    }
    return tiers or None


def parse_role_models(models_section: Mapping[str, Any]) -> dict[str, str]:
    """Parse ``[models.roles]`` — per-agent model pins.

    Each value is either a tier name or a literal model id; the distinction is
    resolved late, in ``BackendConfig.model_for_role``, so a role pinned to a
    tier keeps tracking that tier's ``[models]`` override.

    An unknown role key raises ``BackendConfigError``. Silently ignoring it is
    the one failure mode this knob must not have: the user pins a model, the
    run bills a different one, and nothing says so.
    """
    roles_section = models_section.get("roles", {})
    if not roles_section:
        return {}
    if not isinstance(roles_section, dict):
        raise BackendConfigError(
            f"booley.toml [models] roles must be a table, got {type(roles_section).__name__}; "
            'write it as [models.roles] with one "<role> = <model>" line per agent.'
        )

    role_models: dict[str, str] = {}
    for role, value in roles_section.items():
        if role not in _KNOWN_ROLES:
            raise BackendConfigError(
                f"booley.toml [models.roles] has an unknown role {role!r}; "
                f"valid roles are {', '.join(sorted(_KNOWN_ROLES))}."
            )
        if not isinstance(value, str) or not value.strip():
            raise BackendConfigError(
                f"booley.toml [models.roles] {role}={value!r} is invalid; use a tier name "
                f'({"/".join(_MODEL_TIERS)}) or a model id (e.g. "claude-opus-4-8").'
            )
        role_models[role] = value.strip()
    return role_models


_parse_role_models = parse_role_models


def _parse_agent_and_models(
    data: dict,
    default_auth: str,
) -> tuple[str, dict[str, str] | None, str | None, dict[str, str]]:
    """Parse [agent] and [models] sections.

    Returns (auth, tier_overrides, provider, role_models). ``provider`` is
    ``None`` when undeclared; the caller applies ``_DEFAULT_PROVIDER``.
    ``tier_overrides`` is sparse — see ``_resolve_tier_models``.

    ``[models]`` is read whether or not ``[agent]`` exists: they are
    independent tables, and gating one on the other made every ``[models]``
    setting in an ``[agent]``-less project silently inert.
    """
    agent_section = data.get("agent", {})
    if not isinstance(agent_section, dict):
        agent_section = {}

    provider = _parse_provider(agent_section)
    auth = _parse_auth(agent_section) or default_auth

    models_section = data.get("models", {})
    if not isinstance(models_section, dict):
        logger.warning("booley.toml [models] is not a table; ignoring")
        models_section = {}

    return (auth, _parse_tier_models(models_section), provider, _parse_role_models(models_section))
