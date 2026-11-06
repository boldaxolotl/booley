"""Validate agent, model, sandbox, and concurrency settings."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from booley.config.gateway import (
    GATEWAY_AUTH,
    GATEWAY_MODEL_ENV,
    GatewayModelPin,
    gateway_model_pin_from_env,
)
from booley.config.jobs import SlotCaps, parse_caps
from booley.config.sandbox import SANDBOX_IMAGE, project_sandbox_image
from booley.core.config_paths import resolve_toml

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# The Agent CLIs Booley can run: Claude Code and Codex. A model ID implies its CLI by prefix.
AGENT_CLIS = ("claude", "codex")
_MODEL_PREFIX_CLI = (("claude-", "claude"), ("gpt-", "codex"))

# The CLI used when nothing else names one. Claude — never codex — so a
# missing/dropped config degrades to Booley's actual backend instead of an
# unauthenticated Codex call that 401s.
_DEFAULT_PROVIDER = "claude"

# [agent] auth: which credential each Agent CLI must bill.
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
#
# ``gateway`` routes Codex only. It is honored inside a Sandbox: set by the
# ``BOOLEY_PRIMARY_AUTH=gateway`` hand-off (plus BOOLEY_GATEWAY_URL / BOOLEY_GATEWAY_TOKEN)
# or, for Codex, in ``[agent.auth]``.
_VALID_AUTH_MODES = ("auto", "subscription", "api_key")
_ENV_AUTH_MODES = (*_VALID_AUTH_MODES, GATEWAY_AUTH)
_DEFAULT_AUTH = "auto"

# The agents whose model may be pinned in ``[models]``: each is a Specialist that runs
# as its own `python -m booley.dev_support.<name>` subprocess and re-reads booley.toml,
# so the key must equal ``Specialist.name``. Explicit rather than derived from the MCP
# registry (importing it would drag the endpoint packages into the config layer); a
# cross-check test fails if a new Specialist is added without extending this set.
SPECIALISTS = frozenset({"reviewer", "mutation_tester", "coverage_analyst"})

# Built-in models. Every Specialist takes the same default per Agent CLI; the internal
# model serves Booley's own calls (Doctor memory probe), never a Specialist.
_DEFAULT_SPECIALIST_MODEL = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
_INTERNAL_MODEL = {"claude": "claude-sonnet-5-5", "codex": "gpt-6-luna"}
# Codex Specialist calls keep reasoning effort high (Claude has no per-call effort knob).
_CODEX_EFFORT = "high"

# The retired tier table, kept only to show what a retired tier key used to resolve to.
_RETIRED_TIER_MODELS: dict[str, dict[str, str]] = {
    "claude": {
        "heavy": "claude-fable-5-1",
        "standard": "claude-opus-5-5",
        "light": "claude-sonnet-5-5",
    },
    "codex": {"heavy": "gpt-6-astra", "standard": "gpt-6.1-sol", "light": "gpt-6-luna"},
}


class BackendConfigError(RuntimeError):
    """Agent provider could not be resolved and must not be guessed.

    Raised instead of silently falling back to a backend the run never asked
    for (the old codex default surfaced as a mystery 401 from an
    unauthenticated Codex call).
    """


def _inside_container() -> bool:
    """Detect if we're running inside a Docker container."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def cli_for_model(model: str) -> str | None:
    """The Agent CLI a model ID implies (``claude-*`` or ``gpt-*``), else ``None``."""
    return next((cli for prefix, cli in _MODEL_PREFIX_CLI if model.startswith(prefix)), None)


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Sandbox configuration parsed from booley.toml [sandbox].

    ADR 0028: Booley is container-only — everything runs inside the one
    Sandbox (devcontainer), so there are no per-Flow containers to
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


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Validated agent, model, sandbox, and job settings consumed by Runtime."""

    default_cli: str = _DEFAULT_PROVIDER
    # The Agent CLIs this Project may run; empty means just the default CLI.
    clis: tuple[str, ...] = ()
    # One auth policy per listed CLI; an unlisted CLI reads as ``auto``.
    auth: Mapping[str, str] = field(default_factory=dict)
    # [models]: Specialist name -> pinned model ID. Absent = the default CLI's built-in model.
    specialist_models: Mapping[str, str] = field(default_factory=dict)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    jobs: SlotCaps = field(default_factory=SlotCaps)
    # Gateway-only pins (BOOLEY_GATEWAY_MODEL / _EFFORT) for gateway-routed Codex.
    model_pin: str | None = None
    effort_pin: str | None = None

    def __post_init__(self) -> None:
        """Freeze caller-owned mappings and default the CLI set to the default CLI."""
        object.__setattr__(self, "clis", self.clis or (self.default_cli,))
        object.__setattr__(self, "auth", MappingProxyType(dict(self.auth)))
        object.__setattr__(
            self, "specialist_models", MappingProxyType(dict(self.specialist_models))
        )

    def auth_for(self, cli: str) -> str:
        """The auth policy for *cli*."""
        return self.auth.get(cli, _DEFAULT_AUTH)

    def cli_for_specialist(self, name: str) -> str:
        """The Agent CLI *name* runs on: its pinned model's CLI, else the default CLI."""
        pinned = self.specialist_models.get(name)
        return (pinned and cli_for_model(pinned)) or self.default_cli

    def model_for_specialist(self, name: str) -> str:
        """The model *name* runs on; a gateway pin replaces it for gateway-routed Codex."""
        if self.model_pin and self.cli_for_specialist(name) == "codex":
            return self.model_pin
        return self.specialist_models.get(name) or _DEFAULT_SPECIALIST_MODEL[self.default_cli]

    def cli_for_model_id(self, model: str) -> str:
        """The Agent CLI that serves *model*: the gateway pin runs on Codex, whatever its name."""
        if self.model_pin and model == self.model_pin:
            return "codex"
        return cli_for_model(model) or self.default_cli

    def internal_model(self) -> str:
        """The model for Booley's own calls on the default CLI (never a Specialist's)."""
        if self.model_pin and self.default_cli == "codex":
            return self.model_pin
        return _INTERNAL_MODEL[self.default_cli]

    def model_settings(self) -> dict[str, tuple[str, ...]]:
        """Every distinct Specialist model mapped to the config keys that select it."""
        keys: dict[str, list[str]] = {}
        for name in sorted(SPECIALISTS):
            model = self.model_for_specialist(name)
            gateway = bool(self.model_pin) and model == self.model_pin
            key = GATEWAY_MODEL_ENV if gateway else f"[models] {name}"
            if key not in keys.setdefault(model, []):
                keys[model].append(key)
        return {model: tuple(names) for model, names in keys.items()}

    def effort_for(self, cli: str) -> str | None:
        """Reasoning effort for a Specialist call on *cli*; ``None`` = the CLI's default."""
        return (self.effort_pin or _CODEX_EFFORT) if cli == "codex" else None


_agent_settings: AgentSettings | None = None


def _gateway_pin(auth: Mapping[str, str]) -> GatewayModelPin:
    """The env model/effort pin; empty unless Codex's policy is the gateway."""
    if auth.get("codex") == GATEWAY_AUTH:
        return gateway_model_pin_from_env()
    return GatewayModelPin()


def _unresolved_provider_error() -> BackendConfigError:
    return BackendConfigError(
        "Agent provider unresolved inside container: BOOLEY_PRIMARY_PROVIDER "
        "is unset, no booley.toml with an [agent] provider was found at "
        "BOOLEY_DIR, and BOOLEY_AGENT_APP names no provider. "
        "If you are running a specialist directly (e.g. "
        "`python -m booley.dev_support.reviewer ...`), pin the backend with one "
        "of:\n"
        "  * export BOOLEY_PRIMARY_PROVIDER=claude   # or codex\n"
        '  * add an [agent] provider = "claude" block to booley.toml\n'
        "In a developer run this instead means the propagation broke — "
        "rebuild the sandbox image if it predates the provider hand-off."
    )


@dataclass(frozen=True)
class DeclaredAgent:
    """What ``[agent]`` declares before the environment or ``[models]`` is consulted.

    ``default`` is ``default``, else its ``provider`` alias, else the first of ``clis``;
    ``auth`` is one policy for every listed CLI, or a ``{cli: policy}`` table.
    """

    clis: tuple[str, ...] | None = None
    default: str | None = None
    auth: str | Mapping[str, str] | None = None


def _checked(value: object, key: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise BackendConfigError(
            f"booley.toml [agent] {key}={value!r} is invalid; "
            f"use one of {', '.join(repr(a) for a in allowed)}."
        )
    return str(value)


def parse_agent_section(agent: Mapping[str, Any]) -> DeclaredAgent:
    """Check the shape of ``[agent]``: ``clis``, ``default``/``provider``, ``auth``.

    Environment-free, so every reader agrees on it; the checks that need the effective
    CLI set (an environment hand-off can change it) live in :func:`settings_from_data`.
    """
    raw_clis, clis = agent.get("clis"), None
    if raw_clis is not None:
        if not isinstance(raw_clis, list) or not raw_clis:
            raise BackendConfigError(
                "booley.toml [agent] clis must be a non-empty list of distinct Agent CLIs"
            )
        # Check each entry before comparing them: a nested list or table is unhashable.
        clis = tuple(_checked(cli, "clis", AGENT_CLIS) for cli in raw_clis)
        if len(set(clis)) != len(clis):
            raise BackendConfigError(
                "booley.toml [agent] clis must be a non-empty list of distinct Agent CLIs"
            )
    default, provider = agent.get("default"), agent.get("provider")
    if None not in (default, provider) and default != provider:
        raise BackendConfigError(
            f"booley.toml [agent] default={default!r} conflicts with provider={provider!r}; "
            "provider is the old spelling of default."
        )
    chosen = default if default is not None else provider
    chosen = None if chosen is None else _checked(chosen, "default", AGENT_CLIS)
    return DeclaredAgent(clis, chosen or (clis[0] if clis else None), _parse_auth(agent))


def _parse_auth(agent: Mapping[str, Any]) -> str | Mapping[str, str] | None:
    raw = agent.get("auth")
    if raw is None or isinstance(raw, str):
        return None if raw is None else _checked(raw, "auth", _ENV_AUTH_MODES)
    if not isinstance(raw, dict):
        raise BackendConfigError(
            "booley.toml [agent] auth must be a policy or an [agent.auth] table"
        )
    table = {}
    for cli, policy in raw.items():
        table[_checked(cli, "auth key", AGENT_CLIS)] = _checked(
            policy, f"auth.{cli}", _ENV_AUTH_MODES
        )
        if cli == "claude" and policy == GATEWAY_AUTH:
            raise BackendConfigError(
                "booley.toml [agent.auth] claude='gateway' is invalid: the gateway routes Codex only"
            )
    return table


def resolve_default_cli(declared: DeclaredAgent | None) -> tuple[str, str] | None:
    """The default Agent CLI and its source, or ``None`` when no source names one.

    Precedence (shared by every loader and Doctor): ``BOOLEY_PRIMARY_PROVIDER``, the
    declared default, then ``BOOLEY_AGENT_APP``.
    """
    env_cli = (os.environ.get("BOOLEY_PRIMARY_PROVIDER") or "").strip()
    if env_cli in AGENT_CLIS:
        return env_cli, "BOOLEY_PRIMARY_PROVIDER"
    if env_cli:
        logger.warning("Ignoring unknown BOOLEY_PRIMARY_PROVIDER %r", env_cli)
    if declared is not None and declared.default is not None:
        return declared.default, "[agent] default"
    from booley.config import agent_app

    app = agent_app.resolved_app((os.environ.get("BOOLEY_AGENT_APP") or "").strip())
    return (app, "BOOLEY_AGENT_APP") if app in AGENT_CLIS else None


def _auth_policies(
    declared: DeclaredAgent, clis: tuple[str, ...], default_cli: str, *, lazy: bool
) -> dict[str, str]:
    """One policy per listed CLI: the declared one, then the environment hand-off."""
    auth = declared.auth
    if isinstance(auth, str):
        if auth == GATEWAY_AUTH and "claude" in clis:
            raise BackendConfigError(
                "booley.toml [agent] auth='gateway' routes Codex only but claude is listed; "
                "use an [agent.auth] table: codex = 'gateway'"
            )
        policies = dict.fromkeys(clis, auth)
    else:
        unlisted = [cli for cli in auth or {} if cli not in clis]
        if unlisted:
            raise BackendConfigError(
                f"booley.toml [agent.auth] {unlisted[0]} is not in [agent] clis"
            )
        policies = {cli: (auth or {}).get(cli, _DEFAULT_AUTH) for cli in clis}
    if not _inside_container():  # the gateway is honored in a Sandbox only
        policies = {cli: _DEFAULT_AUTH if p == GATEWAY_AUTH else p for cli, p in policies.items()}
    if gateway_handoff_active():
        if "codex" in policies:
            policies["codex"] = GATEWAY_AUTH
    elif lazy and env_auth_handoff() is not None:
        policies[default_cli] = _env_auth()  # until #2755 hands off per CLI
    return policies


def _retired_models_error(models: Mapping[str, Any], default_cli: str) -> BackendConfigError:
    """The migration error for tier keys and ``[models.roles]``, showing the new entries."""
    tiers = dict(_RETIRED_TIER_MODELS[default_cli])
    tiers.update({t: models[t].strip() for t in tiers if isinstance(models.get(t), str)})
    roles = models.get("roles") if isinstance(models.get("roles"), dict) else {}
    lines, clis, notes = [], [default_cli], []
    for name in sorted(SPECIALISTS):
        pin = roles.get(name)
        model = tiers.get(pin, pin) if isinstance(pin, str) and pin.strip() else tiers["standard"]
        lines.append(f'{name} = "{model}"')
        cli = cli_for_model(model)
        if cli is None:
            notes.append(f"replace the {name} model with a claude-* or gpt-* ID")
        elif cli not in clis:
            clis.append(cli)
    if "heavy" in models or "light" in models:
        notes.append("heavy and light overrides have no equivalent and are dropped")
    agent = f'[agent]\nclis = {clis!r}\ndefault = "{default_cli}"\n'.replace("'", '"')
    return BackendConfigError(
        "booley.toml [models] tier keys and [models.roles] are retired: each Specialist now "
        "has its own [models] key and its model implies the Agent CLI. Replace them with:\n"
        + (agent if len(clis) > 1 else "")
        + "[models]\n"
        + "\n".join(lines)
        + (f"\n({'; '.join(notes)})" if notes else "")
    )


def parse_models_section(
    models: Mapping[str, Any], clis: tuple[str, ...], default_cli: str
) -> dict[str, str]:
    """Check ``[models]`` against the listed CLIs; return the Specialist -> model pins."""
    if {"heavy", "standard", "light", "roles"} & set(models):
        raise _retired_models_error(models, default_cli)
    pins: dict[str, str] = {}
    for name, value in models.items():
        if name not in SPECIALISTS:
            raise BackendConfigError(
                f"booley.toml [models] has an unknown key {name!r}; "
                f"valid keys are {', '.join(sorted(SPECIALISTS))}."
            )
        model = value.strip() if isinstance(value, str) else ""
        cli = cli_for_model(model)
        if cli is None:
            raise BackendConfigError(
                f"booley.toml [models] {name}={value!r} names no known Agent CLI: "
                "claude-* models run on Claude Code and gpt-* models on Codex."
            )
        if cli not in clis:
            raise BackendConfigError(
                f"booley.toml [models] {name}={model!r} needs the {cli} Agent CLI, "
                f"which is not in [agent] clis; add {cli!r}."
            )
        pins[name] = model
    return pins


def settings_from_data(data: Mapping[str, Any], *, lazy: bool) -> AgentSettings:
    """Build and validate settings from a parsed booley.toml, shared by every loader.

    *lazy* is the Specialist-subprocess path: it honors every ``BOOLEY_PRIMARY_AUTH``
    hand-off and refuses to guess an unresolved CLI inside a Sandbox.
    """
    agent = data.get("agent", {})
    declared = parse_agent_section(agent if isinstance(agent, dict) else {})
    resolved = resolve_default_cli(declared)
    if resolved is None and lazy and _inside_container():
        raise _unresolved_provider_error()
    default_cli, source = resolved or (_DEFAULT_PROVIDER, "default")
    clis = declared.clis or (default_cli,)
    if default_cli not in clis:
        raise BackendConfigError(
            f"{source} names {default_cli!r}, which is not in booley.toml [agent] clis."
        )
    policies = _auth_policies(declared, clis, default_cli, lazy=lazy)
    models = data.get("models", {})
    if not isinstance(models, dict):
        logger.warning("booley.toml [models] is not a table; ignoring")
        models = {}
    pin = _gateway_pin(policies)
    return AgentSettings(
        default_cli=default_cli,
        clis=clis,
        auth=policies,
        specialist_models=parse_models_section(models, clis, default_cli),
        model_pin=pin.model,
        effort_pin=pin.effort,
    )


def get_agent_settings() -> AgentSettings:
    """Return validated settings, resolving environment and Project defaults lazily."""
    global _agent_settings
    if _agent_settings is None:
        _agent_settings = settings_from_data(_project_toml_from_env(), lazy=True)
    return _agent_settings


def set_agent_settings(settings: AgentSettings | None) -> None:
    """Replace or clear cached settings for deterministic tests and composition."""
    global _agent_settings
    _agent_settings = settings


def _project_toml_from_env() -> dict[str, Any]:
    """The Project's booley.toml via ``BOOLEY_DIR`` (what a Specialist subprocess sees).

    Container layout: the toml mounted flat in ``BOOLEY_DIR``; host: ``<repo>/.booley``.
    """
    booley_dir = os.environ.get("BOOLEY_DIR")
    if not booley_dir:
        return {}
    base = Path(booley_dir)
    candidates = (base / "booley.toml", base / ".booley" / "booley.toml")
    return next((data for path in candidates if (data := _read_toml(path)) is not None), {})


def _read_toml(path: Path) -> dict[str, Any] | None:
    """The parsed toml at *path*, or ``None`` when it is missing or unreadable."""
    import tomllib

    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Failed to read agent config from %s: %s", path, e)
        return None


def _parse_sandbox_config(data: dict) -> SandboxConfig:
    """Parse [sandbox] from booley.toml (single container memory limit).

    Legacy pre-ADR-0028 memory tier knobs are warned about and ignored; the
    Sandbox has one memory limit.
    """
    section = data.get("sandbox", {})
    if "mode" in section:
        logger.warning("[sandbox].mode is retired: the Sandbox is always Docker; delete it")
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

    return SandboxConfig(
        image=image,
        memory=memory,
        mount_host_skills=mount_host_skills,
    )


def _parse_jobs_config(data: dict) -> SlotCaps:
    """Parse [jobs] concurrency caps (ADR 0028 Decision 5).

    Thin compatibility alias for the Config-owned parser.
    """
    return parse_caps(data)


def _resolve_booley_toml(project_data: Path) -> Path:
    """Resolve booley.toml in *project_data*, warning on legacy pipeline.toml."""
    path = resolve_toml(project_data)
    if path.name == "pipeline.toml" and path.exists():
        logger.warning("Using legacy pipeline.toml — rename to booley.toml")
    return path


def load_agent_settings(project_root: Path, *, booley_dir: Path | None = None) -> AgentSettings:
    """Load and install validated agent, model, sandbox, and job settings.

    The default Agent CLI comes from ``BOOLEY_PRIMARY_PROVIDER``, ``[agent] default``
    or ``BOOLEY_AGENT_APP`` (else Claude); ``[agent] clis`` lists the CLIs a Project may
    run and ``[models]`` pins Specialists. ``booley_dir`` lets callers that already
    resolved the canonical Booley Directory avoid repeating path discovery. Invalid
    ``[agent]`` or ``[models]`` content raises ``BackendConfigError`` — we never
    silently run a backend or model the project didn't ask for.
    """
    global _agent_settings
    data = _read_toml(_resolve_booley_toml(booley_dir or project_root / ".booley"))
    settings = settings_from_data(data or {}, lazy=False)
    if data is not None:
        try:
            sandbox = replace(
                _parse_sandbox_config(data), image=project_sandbox_image(project_root)
            )
            settings = replace(settings, sandbox=sandbox, jobs=_parse_jobs_config(data))
        except (OSError, KeyError, TypeError, ValueError) as e:
            logger.warning("Failed to parse booley.toml: %s (using defaults)", e)
    _agent_settings = settings
    return settings


def env_auth_handoff() -> str | None:
    """The normalized ``BOOLEY_PRIMARY_AUTH`` hand-off, or ``None`` when unset/blank.

    The single consumer-side reader of the variable (the registrar's resolver
    alone reads the raw value, once per start, to publish it), so a Specialist subprocess, the MCP
    server, Doctor and ``booley auth --status`` cannot disagree about spellings
    such as ``"gateway "``. Validity is judged by :func:`_env_auth`.

    Under ``BOOLEY_AGENT_APP=auto`` the value is the policy the registrar
    published for this start, and an unresolved or out-of-Sandbox ``auto``
    raises :class:`~booley.config.agent_app.AgentAppError` (a
    ``BackendConfigError``).
    """
    import os

    from booley.config import agent_app

    choice = agent_app.auto_choice()
    raw = choice.policy if choice else os.environ.get("BOOLEY_PRIMARY_AUTH")
    return (raw or "").strip().lower() or None


def gateway_handoff_active() -> bool:
    """Whether ``BOOLEY_PRIMARY_AUTH=gateway`` is in force.

    Honored only inside a container: a Booley-issued Sandbox always takes its
    policy from ``booley.toml`` and never receives BOOLEY_GATEWAY_*, so an
    export on the host must not silently change what a host-side run bills.
    """
    return env_auth_handoff() == GATEWAY_AUTH and _inside_container()


def _env_auth() -> str:
    """The auth mode from ``BOOLEY_PRIMARY_AUTH``, warning-and-defaulting on junk.

    The env var is a harness hand-off, not user config, so an unknown value is
    degraded to the default with a warning rather than raised (mirrors how
    ``resolve_default_cli`` treats an unknown ``BOOLEY_PRIMARY_PROVIDER``).
    ``gateway`` on a host (outside a container) is treated as unknown.
    """
    raw = env_auth_handoff() or _DEFAULT_AUTH
    if raw not in _ENV_AUTH_MODES or (raw == GATEWAY_AUTH and not _inside_container()):
        logger.warning("Ignoring BOOLEY_PRIMARY_AUTH %r; using %s", raw, _DEFAULT_AUTH)
        return _DEFAULT_AUTH
    return raw


def project_credential_policy(
    project_root: Path, *, booley_dir: Path | None = None
) -> tuple[str, str]:
    """The default CLI and its declared auth policy at an already-selected root.

    Reads booley.toml only (no environment) and fails closed on invalid input.
    """
    import tomllib

    if booley_dir is None:
        from booley.core.booley_dir import resolve_authoritative_booley_dir

        try:
            booley_dir = resolve_authoritative_booley_dir(project_root)
        except FileNotFoundError:
            booley_dir = project_root / ".booley"
    path = _resolve_booley_toml(booley_dir)
    if not path.exists():
        return _DEFAULT_PROVIDER, _DEFAULT_AUTH
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise BackendConfigError(
            "cannot read Project credential policy; repair booley.toml"
        ) from exc
    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        raise BackendConfigError("booley.toml [agent] must be a table")
    declared = parse_agent_section(agent)
    cli = declared.default or _DEFAULT_PROVIDER
    auth = declared.auth
    policies = {auth} if isinstance(auth, str) else set((auth or {}).values())
    if GATEWAY_AUTH in policies:  # whichever listed CLI asks for it, not only the default's
        raise BackendConfigError(
            "a Booley-issued Sandbox cannot route Codex through the gateway yet; "
            "use a hand-written Sandbox spec with BOOLEY_PRIMARY_AUTH=gateway"
        )
    policy = auth if isinstance(auth, str) else (auth or {}).get(cli)
    return cli, policy or _DEFAULT_AUTH
