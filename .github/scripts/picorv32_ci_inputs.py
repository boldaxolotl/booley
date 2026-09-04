"""Authoritative path sets for PicoRV32 CI assurance."""

from __future__ import annotations

PICORV32_INPUT_FILES = frozenset(
    {
        ".github/contracts/picorv32-demo-ticket.md",
        ".github/contracts/picorv32-demo.toml",
        ".github/scripts/export_demo_contract.py",
        ".github/scripts/install_demo_ticket.py",
        ".github/scripts/picorv32_ci_inputs.py",
        ".github/scripts/picorv32_demo_contract.py",
        ".github/scripts/pull_image_identity.py",
        ".github/scripts/verify_picorv32_demo.sh",
        ".github/workflows/picorv32-demo.yml",
        "pyproject.toml",
    }
)

PICORV32_PULL_REQUEST_PATHS = frozenset(
    {
        ".github/actions/prepare-picorv32-demo/**",
        *PICORV32_INPUT_FILES,
        "src/booley/**",
    }
)

RISCV_IMAGE_PREFIXES = (".github/actions/prepare-picorv32-demo/", "demo/")
RISCV_IMAGE_FILES = frozenset(
    {
        ".github/contracts/image-size-limits.toml",
        ".github/contracts/session-runtime.toml",
        ".github/scripts/ci_changes.py",
        ".github/scripts/image_contract.py",
        ".github/scripts/image_runtime_resources.py",
        ".github/scripts/image_size_report.py",
        ".github/workflows/test.yml",
        *PICORV32_INPUT_FILES,
    }
)
