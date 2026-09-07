"""Reproducible evidence for Booley's Vivado PPA-profile mapping."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_FIXTURE = Path(__file__).resolve().parents[4] / "fixtures" / "vivado_profile_characterization"
_REPORT = _FIXTURE.parents[2] / "docs" / "research" / "vivado-profile-characterization-20260907.md"
_PROFILES = ("balanced", "compact", "max_frequency")
_EXPECTED_MAPPING = {
    "balanced": ("Vivado Synthesis Defaults", "Vivado Implementation Defaults"),
    "compact": ("Flow_AreaOptimized_high", "Area_Explore"),
    "max_frequency": (
        "Flow_PerfOptimized_high",
        "Performance_ExplorePostRoutePhysOpt",
    ),
}


def _evidence() -> dict[str, object]:
    return json.loads((_FIXTURE / "evidence.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _marker_values(output: str, marker: str) -> list[str]:
    prefix = f"{marker}="
    return [line.removeprefix(prefix) for line in output.splitlines() if line.startswith(prefix)]


def test_checked_in_evidence_covers_every_portable_profile() -> None:
    evidence = _evidence()
    assert evidence["schema_version"] == 1
    assert evidence["tool"] == {
        "name": "Vivado",
        "version": "2025.2",
        "build": "6299465",
        "ip_build": "6300035",
        "shared_data_build": "6298862",
        "edalize_version": "0.6.8",
    }
    assert evidence["fixture"] == {
        "part": "xc7a35tcpg236-1",
        "top": "top",
        "clock_period_ns": 10.0,
        "mode": "out_of_context",
        "sha256": {
            name: _sha256(_FIXTURE / name) for name in ("top.sv", "top.xdc", "characterize.tcl")
        },
    }
    assert tuple(evidence["profiles"]) == _PROFILES

    supported = evidence["supported_strategies"]
    assert tuple(supported) == ("synthesis", "implementation")
    for catalog in supported.values():
        assert catalog == sorted(set(catalog))
    report = _REPORT.read_text(encoding="utf-8")
    for profile, (synthesis, implementation) in _EXPECTED_MAPPING.items():
        result = evidence["profiles"][profile]
        assert result["synthesis_strategy"] == synthesis
        assert result["implementation_strategy"] == implementation
        assert synthesis in supported["synthesis"]
        assert implementation in supported["implementation"]
        assert result["final_status"].endswith("Complete!")
        assert result["wns_ns"] >= 0
        assert result["whs_ns"] >= 0
        assert "-mode out_of_context" in result["effective_commands"][0]
        assert f"| `{profile}` | `{synthesis}` | `{implementation}` |" in report


def _vivado() -> Path:
    if os.environ.get("BOOLEY_VIVADO_PROFILE_CHARACTERIZATION") != "1":
        pytest.skip("set BOOLEY_VIVADO_PROFILE_CHARACTERIZATION=1 on an approved Vivado host")
    root = os.environ.get("BOOLEY_VIVADO_ROOT", "").strip()
    if not root:
        pytest.fail("BOOLEY_VIVADO_ROOT is required for profile characterization")
    executable = Path(root).resolve() / "Vivado" / "bin" / "vivado"
    if not os.access(executable, os.X_OK):
        pytest.fail("BOOLEY_VIVADO_ROOT is not an executable Vivado release root")
    return executable


@pytest.mark.slow
@pytest.mark.parametrize("profile", _PROFILES)
def test_profile_completes_routed_characterization(profile: str, tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            str(_vivado()),
            "-mode",
            "batch",
            "-source",
            str(_FIXTURE / "characterize.tcl"),
            "-tclargs",
            profile,
            str(tmp_path / profile),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    evidence = _evidence()
    expected = evidence["profiles"][profile]
    tool = evidence["tool"]
    assert f"BOOLEY_VIVADO_VERSION={tool['version']}" in output
    assert f"BOOLEY_VIVADO_BUILD={tool['build']}" in output
    assert (
        _marker_values(output, "BOOLEY_SYNTH_SUPPORTED")
        == evidence["supported_strategies"]["synthesis"]
    )
    assert (
        _marker_values(output, "BOOLEY_IMPL_SUPPORTED")
        == evidence["supported_strategies"]["implementation"]
    )
    assert f"BOOLEY_SYNTH_STRATEGY={expected['synthesis_strategy']}" in output
    assert f"BOOLEY_IMPL_STRATEGY={expected['implementation_strategy']}" in output
    assert "BOOLEY_SYNTH_MORE_OPTIONS=-mode out_of_context" in output
    assert f"BOOLEY_STATUS={expected['final_status']}" in output
    assert re.search(r"BOOLEY_WNS_NS=-?\d+(?:\.\d+)?", output)
    assert re.search(r"BOOLEY_WHS_NS=-?\d+(?:\.\d+)?", output)
