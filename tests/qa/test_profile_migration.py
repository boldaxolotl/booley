"""Protect reviewed profile membership without retaining historical handoff prose."""

import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2] / "qa"


def membership_digest(values):
    return hashlib.sha256(("\n".join(sorted(values)) + "\n").encode()).hexdigest()


def test_every_profile_preserves_exact_reviewed_membership_and_exclusions():
    contract = json.loads(Path(__file__).with_name("profile-contract.json").read_text())[
        "profiles"
    ]
    profiles = yaml.safe_load((ROOT / "profiles.yaml").read_text())["profiles"]
    assert {p["id"] for p in profiles} == contract.keys()
    for profile in profiles:
        expected = contract[profile["id"]]
        assert profile["required"] == expected["required"]
        assert {r["id"] for r in profile["runs"]} == expected["runs"].keys()
        for run in profile["runs"]:
            frozen = expected["runs"][run["id"]]
            assert membership_digest(run["checks"]) == frozen["checks"], run["id"]
            assert membership_digest(x["check"] for x in run["exclusions"]) == frozen["exclusions"]
