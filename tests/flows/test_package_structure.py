"""Architecture tests for built-in Flow package ownership and entry points."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.architecture.production import production_dependencies

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


def test_flow_implementations_are_owned_by_their_flow_packages() -> None:
    package_root = REPO_ROOT / "src" / "booley"
    assert not (package_root / "sim").exists()
    assert not (package_root / "synthesis").exists()
    assert not (package_root / "yosys").exists()
    assert not (package_root / "vivado").exists()

    assert (FLOWS_ROOT / "sim" / "backends").is_dir()
    assert (FLOWS_ROOT / "synth" / "backends" / "yosys").is_dir()
    assert (FLOWS_ROOT / "synth" / "backends" / "openroad").is_dir()
    assert (FLOWS_ROOT / "fpga" / "backends" / "vivado").is_dir()
    assert not (FLOWS_ROOT / "synth" / "configure.py").exists()
    assert not (FLOWS_ROOT / "synth" / "pipeline.py").exists()
    assert (FLOWS_ROOT / "synth" / "backends" / "configure.py").is_file()
    assert (FLOWS_ROOT / "synth" / "backends" / "pipeline.py").is_file()
    assert (FLOWS_ROOT / "synth" / "backends" / "openroad" / "reporting.py").is_file()


def _imported_modules(source_file: Path) -> set[str]:
    resolved = source_file.resolve()
    return {
        dependency.target
        for dependency in production_dependencies()
        if dependency.path == resolved
    }


def test_flow_packages_do_not_import_one_another() -> None:
    for owner in FLOW_PACKAGES:
        forbidden = {f"booley.flows.{other}" for other in FLOW_PACKAGES if other != owner}
        for source_file in (FLOWS_ROOT / owner).rglob("*.py"):
            imported = _imported_modules(source_file)
            violations = {
                module
                for module in imported
                if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
            }
            assert not violations, f"{source_file}: cross-Flow imports {sorted(violations)}"


def test_flow_neutral_modules_do_not_depend_on_concrete_flows() -> None:
    forbidden = {f"booley.flows.{flow}" for flow in FLOW_PACKAGES}
    for source_file in FLOWS_ROOT.glob("*.py"):
        imported = _imported_modules(source_file)
        violations = {
            module
            for module in imported
            if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
        }
        assert not violations, f"{source_file}: neutral module imports {sorted(violations)}"


def test_backend_adapters_do_not_import_sibling_adapters() -> None:
    owners = {
        "sim": {
            "cocotb": ("cocotb.py", "cocotb_results.py"),
            "icarus": ("icarus.py",),
            "verilator": ("verilator.py",),
        },
        "synth": {
            "openroad": ("openroad",),
            "yosys": ("yosys",),
        },
    }
    for flow, adapters in owners.items():
        backends = FLOWS_ROOT / flow / "backends"
        for owner, owned_paths in adapters.items():
            forbidden = {
                f"booley.flows.{flow}.backends.{sibling}"
                for sibling in adapters
                if sibling != owner
            }
            source_files = []
            for relative in owned_paths:
                path = backends / relative
                source_files.extend(path.rglob("*.py") if path.is_dir() else (path,))
            for source_file in source_files:
                imported = _imported_modules(source_file)
                violations = {
                    module
                    for module in imported
                    if any(
                        module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden
                    )
                }
                assert not violations, f"{source_file}: sibling imports {sorted(violations)}"


def test_synth_leaf_backends_do_not_import_flow_or_one_another() -> None:
    backends = FLOWS_ROOT / "synth" / "backends"
    for owner in ("yosys", "openroad"):
        forbidden = {"booley.flows.synth.flow"}
        for source_file in (backends / owner).rglob("*.py"):
            imported = _imported_modules(source_file)
            violations = {
                module
                for module in imported
                if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
            }
            assert not violations, f"{source_file}: backend imports {sorted(violations)}"


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
