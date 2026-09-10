"""Fail-closed Codex composition with no built-in execution capabilities.

Codex 0.153.4 registers apply_patch from model metadata independently of shell
feature flags. A private exact-model catalog removes that metadata; explicit
startup overrides remove the remaining tool registration gates. No user/project
MCP configuration or skills are inherited into the empty analysis workspace.
"""

import json
import shutil
from collections.abc import Mapping
from pathlib import Path

from booley.core.boundary import require_dict, require_list
from booley.core.models import AgentCallParams

from .mcp_config import generate_codex_config

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
) -> tuple[list[str], dict[str, str]]:
    """Build an isolated invocation config; missing exact model metadata is an error."""
    original = Path(
        environment.get("CODEX_HOME") or Path(environment.get("HOME", str(Path.home()))) / ".codex"
    )
    selected = _exact_model(original, params.model)
    model = {
        **selected,
        "shell_type": "disabled",
        "apply_patch_tool_type": None,
        "experimental_supported_tools": [],
        "tool_mode": "direct",
        "node_repl_disabled": True,
        "supports_search_tool": False,
    }
    private = Path(params.cwd) / ".text-only-codex"
    private.mkdir(exist_ok=True, mode=0o700)
    catalog = private / "model-catalog.json"
    catalog.write_text(json.dumps({"models": [model]}), encoding="utf-8")
    if (original / "auth.json").is_file():
        shutil.copyfile(original / "auth.json", private / "auth.json")
        (private / "auth.json").chmod(0o600)
    if params.nested_mcp_tools:
        nested_env = {
            **(params.nested_mcp_env or {}),
            "BOOLEY_NESTED_AGENT": "1",
            "BOOLEY_NESTED_MCP_TOOLS": ",".join(params.nested_mcp_tools),
        }
        (private / "config.toml").write_text(
            generate_codex_config(extra_env=nested_env), encoding="utf-8"
        )
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


def _exact_model(original: Path, name: str) -> dict[str, object]:
    try:
        data = require_dict(
            json.loads((original / "models_cache.json").read_text(encoding="utf-8"))
        )
        matches = [
            model
            for model in require_list(data.get("models"))
            if isinstance(model, dict) and model.get("slug") == name
        ]
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Text-only analysis requires the exact model in the Codex models cache; refresh Codex model metadata first"
        ) from exc
    if len(matches) != 1:
        raise ValueError(
            "Text-only analysis requires exactly one exact model in the Codex models cache"
        )
    return matches[0]
