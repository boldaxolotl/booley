"""Shared input paths for PicoRV32 CI assurance."""

from __future__ import annotations

PICORV32_INPUT_FILES = frozenset(
    {
        ".github/ci/constraints.txt",
        ".github/contracts/picorv32-demo.toml",
        ".github/scripts/export_demo_contract.py",
        ".github/scripts/picorv32_ci_inputs.py",
        ".github/scripts/picorv32_demo_contract.py",
        ".github/scripts/riscv_candidate.sh",
        ".github/scripts/verify_picorv32_demo.sh",
        ".github/scripts/run_picorv32_ci_demo.sh",
        ".github/scripts/release_validation/measure_round_trip.py",
        ".github/scripts/release_validation/measurement_evidence.py",
        ".github/workflows/picorv32-demo.yml",
        "pyproject.toml",
    }
)

RISCV_IMAGE_PREFIXES = (
    ".github/actions/prepare-picorv32-demo/",
    "demo/",
    "src/booley/review/",
    "src/booley/evidence/",
)
RISCV_IMAGE_FILES = frozenset(
    {
        ".github/contracts/image-size-limits.toml",
        ".github/contracts/session-runtime.toml",
        ".github/scripts/ci_changes.py",
        ".github/scripts/image_contract.py",
        ".github/scripts/image_runtime_resources.py",
        ".github/scripts/image_size_report.py",
        ".github/scripts/riscv_tooling.py",
        ".github/scripts/verify_riscv_image_contract.sh",
        ".github/workflows/riscv-tooling-publish.yml",
        ".github/workflows/test.yml",
        "src/booley/runtime/dockerfile_syntax.py",
        *PICORV32_INPUT_FILES,
    }
)
