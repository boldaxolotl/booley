"""Public Booley Flow names and migration aliases."""

from booley.targets.flow_names import canonical, canonicalize_config, config_section


def test_long_names_map_to_short_public_names():
    assert canonical("asic_synthesize") == "synth"
    assert canonical("fpga_impl") == "fpga"
    assert canonical("simulate") == "sim"
    assert canonical("elaborate") == "elaborate"
    assert canonical("lint") == "lint"


def test_config_section_reads_only_the_canonical_spelling():
    legacy_only = {"simulate": {"timeout_ms": 1000}}
    assert config_section(legacy_only, "sim") == {}
    assert config_section({"sim": {"timeout_ms": 2000}, **legacy_only}, "sim") == {
        "timeout_ms": 2000
    }


def test_whole_config_normalization_covers_tables():
    normalized = canonicalize_config(
        {
            "flows": {
                "simulate": {"timeout_ms": 1000},
            }
        }
    )
    assert normalized["flows"]["sim"] == {"timeout_ms": 1000}
    assert "simulate" not in normalized["flows"]
