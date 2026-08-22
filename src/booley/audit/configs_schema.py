"""Typed normalization and validation for project ``configs.toml``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from booley.config.project_config import normalize_configs_toml
from booley.core.boundary import (
    BoundaryError,
    as_dict,
    as_str,
    is_str_list,
    require_bool,
    require_int,
)


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """One actionable configuration-schema violation."""

    message: str
    fix: str


@dataclass(frozen=True, slots=True)
class ConfigsTomlAudit:
    """Normalized configs when valid, plus every schema violation found."""

    configs: dict[str, dict[str, Any]] | None
    issues: tuple[ConfigIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Whether normalized configuration is safe for internal callers."""
        return self.configs is not None


def audit_configs_toml(raw: Mapping[str, Any]) -> ConfigsTomlAudit:
    """Normalize and strictly validate every ``configs.toml`` config section."""
    try:
        normalized = normalize_configs_toml(raw)
    except ValueError as exc:
        return ConfigsTomlAudit(None, (ConfigIssue(str(exc), "fix configs.toml"),))
    if not normalized:
        issue = ConfigIssue(
            "configs.toml has no config sections",
            "add [default] with defines = []",
        )
        return ConfigsTomlAudit(None, (issue,))

    configs: dict[str, dict[str, Any]] = {}
    issues: list[ConfigIssue] = []
    for name, raw_section in normalized.items():
        section = as_dict(raw_section)
        if section is None:
            issues.append(ConfigIssue(f"configs.toml [{name}] must be a table", f"fix [{name}]"))
            continue
        configs[name] = section
        issues.extend(_section_issues(name, section))
    return ConfigsTomlAudit(None if issues else configs, tuple(issues))


def _section_issues(name: str, section: Mapping[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    if not is_str_list(section.get("defines")):
        issues.append(
            ConfigIssue(
                f"configs.toml [{name}].defines must be present as list[str]",
                f"add defines = [] to [{name}]",
            )
        )
    for key in ("top_module", "tb_top"):
        value = section.get(key)
        if value is not None and as_str(value) is None:
            issues.append(
                ConfigIssue(
                    f"configs.toml [{name}].{key} must be a string",
                    f"fix [{name}].{key}",
                )
            )
    tests = section.get("tests")
    if tests is not None and not is_str_list(tests):
        issues.append(
            ConfigIssue(
                f"configs.toml [{name}].tests must be list[str]",
                f"fix [{name}].tests",
            )
        )
    issues.extend(_parameter_issues(name, section.get("parameters")))
    return issues


def _parameter_issues(config_name: str, value: Any) -> list[ConfigIssue]:
    if value is None:
        return []
    parameters = as_dict(value)
    if parameters is None:
        return [
            ConfigIssue(
                f"configs.toml [{config_name}].parameters must be a table",
                f"fix [{config_name}.parameters]",
            )
        ]
    issues: list[ConfigIssue] = []
    for parameter_name, parameter_value in parameters.items():
        label = f"configs.toml [{config_name}.parameters].{parameter_name}"
        name = as_str(parameter_name)
        if name is None or not name.strip():
            issues.append(
                ConfigIssue(
                    f"{label} name must be non-empty",
                    f"fix [{config_name}.parameters]",
                )
            )
            continue
        issue = _parameter_value_issue(label, parameter_value)
        if issue is not None:
            issues.append(issue)
    return issues


def _parameter_value_issue(label: str, value: Any) -> ConfigIssue | None:
    if _is_bool(value) or _is_int(value, label):
        return None
    if as_str(value) is not None:
        return ConfigIssue(
            f"{label} plain strings are not allowed",
            'use { expr = "..." } or { string = "..." }',
        )
    table = as_dict(value)
    if table is not None:
        return _parameter_table_issue(label, table)
    return ConfigIssue(
        f'{label} must be bool, int, {{ expr = "..." }}, or {{ string = "..." }}',
        f"fix {label}",
    )


def _is_bool(value: Any) -> bool:
    try:
        require_bool({"value": value}, "value")
    except BoundaryError:
        return False
    return True


def _is_int(value: Any, label: str) -> bool:
    try:
        require_int(value, field=label)
    except BoundaryError:
        return False
    return True


def _parameter_table_issue(label: str, value: Mapping[str, Any]) -> ConfigIssue | None:
    keys = set(value)
    if keys == {"expr"}:
        expression = as_str(value["expr"])
        if expression is not None and expression.strip():
            return None
        return ConfigIssue(f"{label}.expr must be a non-empty string", f"fix {label}")
    if keys == {"string"}:
        if as_str(value["string"]) is not None:
            return None
        return ConfigIssue(f"{label}.string must be a string", f"fix {label}")
    return ConfigIssue(
        f"{label} table must contain exactly one key: expr or string",
        f"fix {label}",
    )
