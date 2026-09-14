"""Compatibility imports for EDA configuration.

Declarative requests live in Config. Host validation and composed loading stay
in EDA provisioning; canonical callers import those owners directly.
"""

from booley.config.eda import (
    PROVISIONING_HOST,
    PROVISIONING_IMAGE,
    SUPPORTED_EDA_KINDS,
    EdaConfig,
    EdaConfigError,
    parse_eda_config,
)
from booley.config.flow_enablement import retired_config_error
from booley.eda.provisioning.authority import installation_name_error
from booley.eda.provisioning.configuration import (
    load_eda_config,
    validate_host_provisioning_platform,
)

__all__ = [
    "PROVISIONING_HOST",
    "PROVISIONING_IMAGE",
    "SUPPORTED_EDA_KINDS",
    "EdaConfig",
    "EdaConfigError",
    "installation_name_error",
    "load_eda_config",
    "parse_eda_config",
    "retired_config_error",
    "validate_host_provisioning_platform",
]
