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


_MEMINFO = "MemTotal:       16384000 kB\nMemFree: 1 kB\nSwapTotal:       2097152 kB\n"


def test_parse_meminfo_reads_memory_and_swap_in_bytes() -> None:
    host = resource_policy.parse_meminfo(_MEMINFO)
    assert host == resource_policy.HostMemory(16384000 * 1024, 2 * _GIB)
    with pytest.raises(ValueError, match="SwapTotal"):
        resource_policy.parse_meminfo("MemTotal: 1 kB\n")
    with pytest.raises(ValueError, match="MemTotal"):
        resource_policy.parse_meminfo("SwapTotal: 1 kB\n")


def test_configured_sandbox_swap_reads_the_section() -> None:
    assert resource_policy.configured_sandbox_swap({"sandbox": {"swap": " 2g "}}) == "2g"
    assert resource_policy.configured_sandbox_swap({"sandbox": {"swap": 3}}) == ""
    assert resource_policy.configured_sandbox_swap({"sandbox": "invalid"}) == ""


_SWAP_ROWS = [
    ("12g", "", 16 * _GIB, "15g"),
    ("12g", "", 1 * _GIB, "13g"),
    ("12g", "", 0, "12g"),
    ("12g", "lots", 16 * _GIB, "15g"),
    ("12g", "0", 16 * _GIB, "12g"),
    ("12g", "2g", 0, "14g"),
    ("1000m", "", 16 * _GIB, "1250m"),
    ("1000k", "", 16 * _GIB, "1250k"),
    ("1001", "", 16 * _GIB, "1251"),
    ("", "2g", 16 * _GIB, ""),
    ("lots", "2g", 16 * _GIB, ""),
]


def test_memory_swap_limit() -> None:
    """The default swap is a quarter of memory, capped at the host's swap."""
    for memory, swap, host_swap, expected in _SWAP_ROWS:
        assert resource_policy.memory_swap_limit(memory, swap, host_swap) == expected, (
            memory,
            swap,
            host_swap,
        )


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
