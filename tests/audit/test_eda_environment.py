"""Focused regression tests for containerized EDA environment audits."""

import ast
from pathlib import Path

from tests.command_runner_fake import CannedRunner, refusing_runner

from booley.audit import eda_environment

_ROOT = Path(__file__).resolve().parents[2]


def test_non_riscv_image_has_no_riscv_probe_surface() -> None:
    findings = eda_environment.audit_riscv_toolchain(
        "docker",
        "booley-sandbox",
        None,
        run=refusing_runner,
    )

    assert findings == ()


def test_riscv_image_probes_every_advertised_tool_and_document() -> None:
    runner = CannedRunner()

    findings = eda_environment.audit_riscv_toolchain(
        "docker",
        "booley-sandbox-riscv",
        "riscv",
        run=runner,
    )

    assert len(findings) == len(eda_environment.riscv_probe_specs())
    assert all(finding.severity is eda_environment.EdaFindingSeverity.PASS for finding in findings)
    document_call = next(call for call in runner.calls if "riscv-isa-unprivileged.html" in call)
    assert set(eda_environment.RISCV_DOC_FILES) <= set(document_call)


def test_failed_riscv_probe_has_one_stable_rebuild_fix() -> None:
    findings = eda_environment.audit_riscv_toolchain(
        "docker",
        "booley-sandbox-riscv",
        "riscv",
        run=CannedRunner(1),
    )

    assert findings
    assert all(finding.severity is eda_environment.EdaFindingSeverity.FAIL for finding in findings)
    assert {finding.fix for finding in findings} == {eda_environment.RISCV_IMAGE_FIX}


def test_eda_environment_does_not_depend_on_presentation_layers() -> None:
    module_path = _ROOT / "src" / "booley" / "audit" / "eda_environment.py"
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


def test_doctor_does_not_reimplement_riscv_probe_inventory() -> None:
    source = (_ROOT / "src" / "booley" / "harness" / "doctor.py").read_text(encoding="utf-8")

    assert 'riscv32-unknown-elf-gcc", "--version' not in source
    assert 'for name in "$@"' not in source
