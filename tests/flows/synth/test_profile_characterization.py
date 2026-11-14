"""Reproducible evidence for Booley's synthesis PPA-profile mappings.

The checked-in measurement (``tests/fixtures/synth_profile_characterization``)
is one design at one clock period. The ordinary lane pins the profile tables to
that data without running EDA tools; the opt-in lane repeats the measurement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from booley.flows.synth.backends.openroad.ppa import openroad_profile
from booley.flows.synth.backends.yosys.ppa import yosys_profile

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "synth_profile_characterization"
_PROFILES = ("compact", "balanced", "max_frequency")


def _evidence() -> dict[str, Any]:
    return json.loads((_FIXTURE / "evidence.json").read_text(encoding="utf-8"))


def _text_sha256(path: Path) -> str:
    """Hash UTF-8 fixture text with checkout-independent line endings."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def test_evidence_covers_every_profile_before_and_after() -> None:
    evidence = _evidence()
    assert evidence["schema_version"] == 1
    for section in ("before", "after"):
        assert tuple(evidence[section]) == _PROFILES
        for result in evidence[section].values():
            assert result["area_kge"] > 0
            assert result["critical_path_ps"] > 0
            assert result["runtime_s"] > 0


@pytest.mark.parametrize("profile", _PROFILES)
def test_after_settings_equal_current_profile_tables(profile: str) -> None:
    """Editing a profile without re-measuring it fails here."""
    expected = {
        "yosys": asdict(yosys_profile(profile)),
        "openroad": asdict(openroad_profile(profile)),
    }
    assert _evidence()["after"][profile]["settings"] == expected


def test_max_frequency_has_shorter_critical_path_than_balanced() -> None:
    evidence = _evidence()
    before = evidence["before"]
    after = evidence["after"]
    # The bug: the old profile was slower than the default one.
    assert before["max_frequency"]["critical_path_ps"] > before["balanced"]["critical_path_ps"]
    assert after["max_frequency"]["critical_path_ps"] < after["balanced"]["critical_path_ps"]
    # compact and balanced mapping are untouched by the change.
    for profile in ("compact", "balanced"):
        assert after[profile]["critical_path_ps"] == before[profile]["critical_path_ps"]
        assert after[profile]["area_kge"] == before[profile]["area_kge"]


def test_clock_period_does_not_quite_close_for_balanced() -> None:
    evidence = _evidence()
    period_ns = evidence["design"]["clock_period_ns"]
    assert -0.10 * period_ns < evidence["before"]["balanced"]["wns_ns"] < 0


def test_period_delay_target_is_slower_than_balanced() -> None:
    """Pin the documented reason ``max_frequency`` sets no delay target."""
    evidence = _evidence()
    period_ps = round(evidence["design"]["clock_period_ns"] * 1000)
    baseline = evidence["after"]["balanced"]["critical_path_ps"]
    targeted = [c for c in evidence["candidates"] if c["delay_target_ps"] == period_ps]
    assert targeted
    assert all(c["critical_path_ps"] > baseline for c in targeted)
    selected = next(c for c in evidence["candidates"] if c["id"] == evidence["selected_candidate"])
    assert selected["delay_target_ps"] is None
    assert selected["critical_path_ps"] == evidence["after"]["max_frequency"]["critical_path_ps"]


def test_fixture_sdc_matches_recorded_digest_and_period() -> None:
    design = _evidence()["design"]
    sdc = _FIXTURE / design["sdc"]
    assert _text_sha256(sdc) == design["sdc_sha256"]
    match = re.search(r"-period\s+([0-9.]+)", sdc.read_text(encoding="utf-8"))
    assert match is not None
    assert float(match.group(1)) == design["clock_period_ns"]


def _project() -> Path:
    if os.environ.get("BOOLEY_SYNTH_PROFILE_CHARACTERIZATION") != "1":
        pytest.skip("set BOOLEY_SYNTH_PROFILE_CHARACTERIZATION=1 to repeat the measurement")
    root = os.environ.get("BOOLEY_PICORV32_PROJECT", "").strip()
    if not root:
        pytest.fail("BOOLEY_PICORV32_PROJECT is required for profile characterization")
    return Path(root).resolve()


@pytest.mark.slow
@pytest.mark.parametrize("profile", _PROFILES)
def test_profile_reproduces_checked_in_measurement(profile: str) -> None:
    # Kept on subprocess.run: opt-in physical synthesis takes minutes, beyond the shared budget.
    project = _project()
    completed = subprocess.run(
        [
            "booley",
            "flow",
            "synth",
            "--target",
            "synth_core",
            "--ppa-profile",
            profile,
            "--no-cache",
            "--report-dir",
            str(project / f"characterization_{profile}"),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(
        (project / f"characterization_{profile}" / "synth_synth_core.json").read_text(
            encoding="utf-8"
        )
    )
    expected = _evidence()["after"][profile]
    clock = report["per_clock"]["clk"]
    assert clock["critical_path_ps"] == pytest.approx(expected["critical_path_ps"], rel=0.02)
    assert report["area_kge"] == pytest.approx(expected["area_kge"], rel=0.02)
