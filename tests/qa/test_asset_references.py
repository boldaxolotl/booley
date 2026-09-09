"""Keep every scenario/shared asset attached to the steps that disclose it."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2] / "qa"


def test_production_assets_have_explicit_owning_steps():
    assets = set()
    for path in ROOT.glob("scenarios/*/scenario.yaml"):
        scenario = yaml.safe_load(path.read_text())
        for step in scenario["steps"]:
            for asset in step.get("assets", []):
                base = ROOT / "shared" if asset.get("base") == "shared" else path.parent
                assets.add((base / asset["path"]).resolve())
    files = {
        path.resolve()
        for directory in ["scenarios", "shared"]
        for path in (ROOT / directory).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.name != "scenario.yaml"
    }
    assert files == assets, sorted(str(p.relative_to(ROOT)) for p in files - assets)


def test_windows_checkout_preserves_hashed_qa_bytes():
    import subprocess

    # Exercise Git's real checkout filters even when the test host is Linux.
    for name in [
        "qa/scenarios/uart/spec/corpus/hw/ip/uart/README.md",
        "qa/scenarios/uart/evaluator/controls.py",
        "qa/shared/probes/H-01.md",
        "qa/scenarios/taxi/fixtures/fixture.core",
    ]:
        committed = subprocess.run(
            ["git", "show", f"HEAD:{name}"],
            cwd=ROOT.parent,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        checkout = subprocess.run(
            ["git", "-c", "core.autocrlf=true", "cat-file", "--filters", f"HEAD:{name}"],
            cwd=ROOT.parent,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        assert checkout == committed, f"Checkout conversion changed hashed QA asset: {name}"
