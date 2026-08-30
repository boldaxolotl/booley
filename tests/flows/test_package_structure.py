"""Architecture tests for built-in Flow package ownership and entry points."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOWS_ROOT = REPO_ROOT / "src" / "booley" / "flows"
FLOW_PACKAGES = ("sim", "synth", "fpga", "lint")
OLD_FLOW_MODULES = (
    "simulate.py",
    "sim_edam.py",
    "asic_synthesize.py",
    "synth_ppa_config.py",
    "synthesis_recipe.py",
    "threshold_eval.py",
    "fpga_impl.py",
    "fpga_cache.py",
    "fpga_edam.py",
    "fpga_metrics.py",
    "lint.py",
)


def test_each_builtin_flow_has_an_executable_package() -> None:
    for package_name in FLOW_PACKAGES:
        package_dir = FLOWS_ROOT / package_name
        assert (package_dir / "__init__.py").is_file()
        assert (package_dir / "__main__.py").is_file()
        assert (package_dir / "flow.py").is_file()


def test_old_flat_flow_modules_are_absent() -> None:
    assert not [module for module in OLD_FLOW_MODULES if (FLOWS_ROOT / module).exists()]
    assert not (REPO_ROOT / "src" / "booley" / "synthesis_profiles.py").exists()


def _imported_modules(source_file: Path) -> set[str]:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 2:
                imported.add(f"booley.flows.{node.module}")
            elif node.level == 0:
                imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_flow_packages_do_not_import_one_another() -> None:
    for owner in FLOW_PACKAGES:
        forbidden = {f"booley.flows.{other}" for other in FLOW_PACKAGES if other != owner}
        for source_file in (FLOWS_ROOT / owner).glob("*.py"):
            imported = _imported_modules(source_file)
            violations = {
                module
                for module in imported
                if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
            }
            assert not violations, f"{source_file}: cross-Flow imports {sorted(violations)}"


@pytest.mark.parametrize("package_name", FLOW_PACKAGES)
def test_package_module_entry_point_has_help(package_name: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-m", f"booley.flows.{package_name}", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
