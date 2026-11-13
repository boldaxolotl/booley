"""Focused regression tests for memory resource policy."""

import ast
from pathlib import Path

import pytest

from booley.audit import resource_policy

_ROOT = Path(__file__).resolve().parents[2]
_GIB = resource_policy.GIB_BYTES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6g", 6 * _GIB),
        ("512M", 512 * 1024**2),
        ("2048", 2048),
        ("lots", None),
    ],
)
def test_parse_memory_limit(text: str, expected: int | None) -> None:
    assert resource_policy.parse_memory_limit(text) == expected


def test_configured_sandbox_memory_accepts_current_and_legacy_shapes() -> None:
    assert resource_policy.configured_sandbox_memory({"sandbox": {"memory": " 8g "}}) == "8g"
    assert (
        resource_policy.configured_sandbox_memory({"sandbox": {"memory": {"default": "12g"}}})
        == "12g"
    )
    assert resource_policy.configured_sandbox_memory({"sandbox": "invalid"}) == ""


def test_cgroup_limit_handles_v2_and_v1_unlimited_values(tmp_path: Path, monkeypatch) -> None:
    invalid = tmp_path / "memory.max"
    invalid.write_text("invalid", encoding="utf-8")
    v1 = tmp_path / "memory.limit_in_bytes"
    v1.write_text(str(1 << 62), encoding="utf-8")
    monkeypatch.setattr(resource_policy, "CGROUP_MEMORY_LIMIT_PATHS", (invalid, v1))

    assert resource_policy.cgroup_memory_limit_bytes() is None

    invalid.write_text(str(9 * _GIB), encoding="utf-8")
    assert resource_policy.cgroup_memory_limit_bytes() == 9 * _GIB


def test_doctor_does_not_reimplement_extracted_resource_mechanisms() -> None:
    doctor_path = _ROOT / "src" / "booley" / "harness" / "doctor.py"
    tree = ast.parse(doctor_path.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    extracted = {
        "_cgroup_memory_limit_bytes",
        "_configured_sandbox_memory",
        "_fmt_g",
        "_parse_mem_limit",
    }
    assert not function_names & extracted


def test_resource_policy_does_not_depend_on_presentation_layers() -> None:
    module_path = _ROOT / "src" / "booley" / "audit" / "resource_policy.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    )

    forbidden = ("booley.harness", "booley.mcp", "booley.specialists")
    assert not {
        module for module in imports if any(module.startswith(prefix) for prefix in forbidden)
    }


def test_memory_budget_override_parses_size() -> None:
    assert (
        resource_policy.configured_memory_budget({"jobs": {"memory_budget": "24g"}}) == 24 * _GIB
    )
    assert resource_policy.configured_memory_budget({"jobs": {"memory_budget": " 512M "}}) == (
        512 * 1024**2
    )


@pytest.mark.parametrize("config", [{}, {"jobs": {}}, {"jobs": []}, {"jobs": {"max_light": 2}}])
def test_memory_budget_override_is_absent_without_the_key(config: dict) -> None:
    assert resource_policy.configured_memory_budget(config) is None


def test_memory_budget_override_rejects_invalid_values() -> None:
    for value in ["0", "0g", "-1g", "1.5g", "abc", "", 0, -5, 24, 2.5, True]:
        with pytest.raises(ValueError, match=r"\[jobs\]\.memory_budget"):
            resource_policy.configured_memory_budget({"jobs": {"memory_budget": value}})
