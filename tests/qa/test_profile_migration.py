"""Keep production selections lossless against the separately reviewed migration record."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2] / "qa"


def reviewed_lists():
    sections = re.split(r"^### ([\w-]+)\n", (ROOT / "handoff/profiles.md").read_text(), flags=re.M)
    result = {}
    for name, body in zip(sections[1::2], sections[2::2], strict=True):
        block = re.search(r"```text\n(.*?)\n```", body, re.S)
        result[name] = set(block[1].splitlines()) if block else set()
    return result


def test_every_profile_preserves_exact_reviewed_membership_and_exclusions():
    lists = reviewed_lists()
    profiles = yaml.safe_load((ROOT / "profiles.yaml").read_text())["profiles"]
    assert {p["id"] for p in profiles} == {
        "core-ubuntu-codex",
        "core-windows-codex",
        "gui-ubuntu-codex",
        "gui-windows-codex",
        "core-ubuntu-claude",
        "gui-ubuntu-claude",
        "gui-windows-claude",
    }
    for profile in profiles:
        assert profile["required"] == (profile["id"] != "gui-windows-claude")
        windows = "windows" in profile["id"]
        gui = profile["id"].startswith("gui")
        for run in profile["runs"]:
            journey = run["scenario_id"].split("-")[0]
            if journey == "opentitan":
                journey = "uart"
            if run["provider"] == "Claude":
                selected = lists["claude-windows-core" if windows else "claude-ubuntu-core"].copy()
                if gui:
                    selected |= lists["claude-supported-client"]
            else:
                names = [
                    f"{journey}-journey-common",
                    f"{journey}-supplement-common",
                    f"{journey}-supplement-windows" if windows else f"{journey}-journey-linux",
                ]
                if not windows:
                    names.append(f"{journey}-supplement-linux")
                if journey == "taxi":
                    names.append("taxi-submodule-companion")
                if gui:
                    names += [f"{journey}-journey-gui", f"{journey}-supplement-gui"]
                selected = set().union(*(lists[name] for name in names))
            assert set(run["checks"]) == selected, run["id"]
            excluded = (
                lists[f"{journey}-supplement-windows"]
                if not windows
                else (lists[f"{journey}-journey-linux"] | lists[f"{journey}-supplement-linux"])
            )
            assert {item["check"] for item in run["exclusions"]} == excluded, run["id"]
