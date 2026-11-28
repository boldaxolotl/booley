"""Migration diagnostics shared by synthesis validation and Doctor."""

from __future__ import annotations


def retired_option_message(key: str, field_prefix: str) -> str:
    """Name the exact replacement for one recognized retired recipe option."""
    if key == "timing_engine":
        return (
            f"{field_prefix}.timing_engine is retired; replace it "
            "with flow_options.synth_mode = physical or logical"
        )
    replacements = {
        "yosys": "advanced_settings_yosys",
        "openroad": "advanced_settings_openroad",
    }
    return f"{field_prefix}.{key} is retired; rename it to {field_prefix}.{replacements[key]}"
