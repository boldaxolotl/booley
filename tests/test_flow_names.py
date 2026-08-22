"""Public Booley Flow names and migration aliases."""

from booley.targets.flow_names import canonical, canonicalize_config, config_section


def test_long_names_map_to_short_public_names():
    assert canonical("asic_synthesize") == "synth"
    assert canonical("fpga_impl") == "fpga"
    assert canonical("simulate") == "sim"
    assert canonical("elaborate") == "elab"
    assert canonical("lint") == "lint"


def test_legacy_config_section_is_not_read():
    flows = {"simulate": {"timeout_ms": 1000}}
    assert config_section(flows, "sim") == {}


def test_canonical_config_wins_when_both_spellings_exist():
    flows = {
        "sim": {"timeout_ms": 2000},
        "simulate": {"timeout_ms": 1000},
    }
    assert config_section(flows, "sim") == {"timeout_ms": 2000}


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
