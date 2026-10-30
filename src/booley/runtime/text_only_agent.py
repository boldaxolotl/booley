"""Fail-closed Codex composition with no built-in execution capabilities.

Codex 0.160.0 registers apply_patch from model metadata independently of shell
feature flags. A private exact-model catalog removes that metadata; explicit
startup overrides remove the remaining tool registration gates. No user/project
MCP configuration or skills are inherited into the empty analysis workspace.
The catalog entry is generated, not read from the models cache, under the
gateway policy (that cache comes from the native login the policy excludes) and
whenever the cache lacks the model.
"""

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from booley.config.gateway import GATEWAY_AUTH, gateway_settings_from_env
from booley.core.boundary import require_dict, require_list
from booley.core.models import AgentCallParams

from .mcp_config import generate_codex_config

logger = logging.getLogger(__name__)

# Booley's system prompt already arrives in the stdin prompt.
_GATEWAY_BASE_INSTRUCTIONS = (
    "Follow the instructions in the user message. Use only the tools offered to you."
)

_DISABLED_FEATURES = (
    "shell_tool",
    "multi_agent",
    "multi_agent_v2",
    "apps",
    "plugins",
    "tool_suggest",
    "view_image",
    "js_repl",
    "code_mode",
    "code_mode_only",
    "memories",
    "image_generation",
    "exec_permission_approvals",
    "request_permissions_tool",
    "deferred_executor",
    "current_time_reminder",
    "sleep_tool",
    "token_budget",
    "goals",
)


def prepare_codex_text_only(
    command: list[str],
    params: AgentCallParams,
    environment: Mapping[str, str],
    *,
    auth_mode: str = "auto",
) -> tuple[list[str], dict[str, str]]:
    """Build an isolated invocation config; a model absent from the cache gets a generated entry."""
    original = Path(
        environment.get("CODEX_HOME") or Path(environment.get("HOME", str(Path.home()))) / ".codex"
    )
    # Fails loud, naming the variable, before any file is written.
    gateway_url = gateway_settings_from_env(environment).url if auth_mode == GATEWAY_AUTH else None
    selected = None if gateway_url else _cached_model(original, params.model)
    if selected is None:
        if not gateway_url:
            logger.warning(
                "Codex models cache lacks %r; using Booley's generated tool-free catalog entry",
                params.model,
            )
        selected = _generated_model(params.model, params.reasoning_effort)
    private = Path(params.cwd) / ".text-only-codex"
    private.mkdir(exist_ok=True, mode=0o700)
    catalog = _write_catalog(private, selected)
    from .auth_token import link_codex_auth

    link_codex_auth(original / "auth.json", private / "auth.json", auth_mode)
    if params.nested_mcp_tools or gateway_url:
        _write_config(private, params, gateway_url)
    env = {**environment, "CODEX_HOME": str(private)}
    settings = [
        f"model_catalog_json={json.dumps(str(catalog))}",
        'web_search="disabled"',
        "agents.enabled=false",
        "tools.update_plan.enabled=false",
        "tools.experimental_request_user_input.enabled=false",
        "project_doc_max_bytes=0",
        *[f"features.{feature}=false" for feature in _DISABLED_FEATURES],
    ]
    overrides = [part for setting in settings for part in ("-c", setting)]
    return [*command[:-1], "--skip-git-repo-check", *overrides, command[-1]], env


def _write_catalog(private: Path, selected: Mapping[str, object]) -> Path:
    """Write the one-model catalog with every tool capability forced off."""
    model = {
        **selected,
        "shell_type": "disabled",
        "apply_patch_tool_type": None,
        "experimental_supported_tools": [],
        "tool_mode": "direct",
        "node_repl_disabled": True,
        "supports_search_tool": False,
    }
    catalog = private / "model-catalog.json"
    catalog.write_text(json.dumps({"models": [model]}), encoding="utf-8")
    return catalog


def _write_config(private: Path, params: AgentCallParams, gateway_url: str | None) -> None:
    """Write ``config.toml``: the nested MCP server (if any) and, under the gateway, its provider."""
    nested_env = {
        **(params.nested_mcp_env or {}),
        "BOOLEY_NESTED_AGENT": "1",
        "BOOLEY_NESTED_MCP_TOOLS": ",".join(params.nested_mcp_tools or ()),
    }
    config_path = private / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    config_path.write_text(
        generate_codex_config(
            extra_env=nested_env,
            existing_config=existing,
            gateway_url=gateway_url,
            gateway_cwd=str(private),
            booley_server=bool(params.nested_mcp_tools),
        ),
        encoding="utf-8",
    )


def _generated_model(name: str, effort: str | None) -> dict[str, object]:
    """Catalog entry for *name* when the gateway or a cache without it supplies none.

    Mirrors Codex ``model_info_from_slug`` (rust-v0.160.0, the fallback for an
    unknown slug) with three deviations: the tool fields (forced off by
    ``_write_catalog``), the reasoning levels (the configured *effort*), and a
    short ``base_instructions`` instead of Codex's coding-agent prompt. Every
    field without a serde default in ``ModelInfo`` is present.
    """
    return {
        "slug": name,
        "display_name": name,
        "description": None,
        "supported_reasoning_levels": (
            [{"effort": effort, "description": effort}] if effort else []
        ),
        **({"default_reasoning_level": effort} if effort else {}),
        "visibility": "none",
        "supported_in_api": True,
        "priority": 99,
        "availability_nux": None,
        "upgrade": None,
        "support_verbosity": False,
        "default_verbosity": None,
        "truncation_policy": {"mode": "bytes", "limit": 10_000},
        "context_window": 272_000,
        "max_context_window": 272_000,
        "effective_context_window_percent": 95,
        "base_instructions": _GATEWAY_BASE_INSTRUCTIONS,
    }


def _cached_model(original: Path, name: str) -> dict[str, object] | None:
    """The cache's entry for *name*, or None when the cache is absent or lacks the model.

    An unreadable, malformed or ambiguous cache is a fault, not a missing model.
    """
    try:
        data = require_dict(
            json.loads((original / "models_cache.json").read_text(encoding="utf-8"))
        )
        matches = [
            model
            for model in require_list(data.get("models"))
            if isinstance(model, dict) and model.get("slug") == name
        ]
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Text-only analysis requires the exact model in the Codex models cache; refresh Codex model metadata first"
        ) from exc
    if len(matches) > 1:
        raise ValueError(
            "Text-only analysis requires exactly one exact model in the Codex models cache"
        )
    return matches[0] if matches else None
