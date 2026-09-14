"""Host-platform policy applied to declarative Project EDA requests."""

from __future__ import annotations

import sys
from pathlib import Path

from booley.config.eda import PROVISIONING_HOST, EdaConfig, EdaConfigError
from booley.config.eda import load_eda_config as load_eda_requests


def validate_host_provisioning_platform(
    configs: dict[str, EdaConfig],
    *,
    platform_name: str | None = None,
) -> None:
    """Reject host-provisioned EDA on unsupported Windows hosts."""
    current_platform = sys.platform if platform_name is None else platform_name
    if current_platform != "win32":
        return
    host_kinds = sorted(
        kind for kind, config in configs.items() if config.provisioning == PROVISIONING_HOST
    )
    if not host_kinds:
        return
    sections = ", ".join(f"[eda.{kind}]" for kind in host_kinds)
    raise EdaConfigError(
        f"host provisioning is unsupported on Windows for {sections}; "
        'set provisioning = "image" or run Booley on a supported Linux x86-64 host'
    )


def load_eda_config(project_root: Path) -> dict[str, EdaConfig]:
    """Load Project requests and enforce host provisioning compatibility."""
    configs = load_eda_requests(project_root)
    validate_host_provisioning_platform(configs)
    return configs
