from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/image_contract.py"
CONTRACT = Path(__file__).parents[2] / ".github/contracts/session-runtime.toml"
SPEC = importlib.util.spec_from_file_location("image_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
image_contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(image_contract)


def test_repository_contract_distinguishes_standard_and_riscv_capabilities() -> None:
    standard = image_contract.load_contract(CONTRACT, "standard")
    riscv = image_contract.load_contract(CONTRACT, "riscv")

    assert "gcc" in standard["required_commands"]
    assert "rustc" not in standard["required_commands"]
    assert "/usr/local/cargo" in standard["absent_paths"]
    assert "riscv-none-elf-gcc" not in standard["required_commands"]
    assert "riscv-none-elf-gcc" in riscv["required_commands"]
    assert len(riscv["probes"]) > len(standard["probes"])


def test_contract_rejects_single_path_hard_link_group(tmp_path: Path) -> None:
    contract = tmp_path / "contract.toml"
    contract.write_text(
        'schema = 1\n[common]\nhard_link_groups = [["/one"]]\n[standard]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="at least two paths"):
        image_contract.load_contract(contract, "standard")


@pytest.mark.skipif(sys.platform == "win32", reason="runtime image probe requires Linux bash")
def test_container_probe_records_absence_hard_links_and_behavior(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("same bytes", encoding="utf-8")
    os.link(first, second)
    contract = {
        "required_commands": ["sh"],
        "required_paths": [str(first)],
        "absent_paths": [str(tmp_path / "absent")],
        "stripped_elf": [],
        "hard_link_groups": [[str(first), str(second)]],
        "probes": [{"name": "shell", "command": "test 2 -eq 2", "timeout_seconds": 5}],
    }

    result = subprocess.run(
        [sys.executable, "-c", image_contract._CONTAINER_PROBE],
        input=json.dumps(contract),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert report["errors"] == []
    assert report["hard_links"][0]["same_inode"] is True
    assert report["absent_paths"] == [{"path": str(tmp_path / "absent"), "absent": True}]
    assert report["probes"][0]["returncode"] == 0


def test_container_probe_reports_missing_hard_link_without_losing_evidence(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first.write_text("bytes", encoding="utf-8")
    missing = tmp_path / "missing"
    contract = {
        "required_commands": [],
        "required_paths": [],
        "absent_paths": [],
        "stripped_elf": [],
        "hard_link_groups": [[str(missing), str(first)]],
        "probes": [],
    }

    result = subprocess.run(
        [sys.executable, "-c", image_contract._CONTAINER_PROBE],
        input=json.dumps(contract),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert report["hard_links"][0]["same_inode"] is False
    assert any("hard-link path is unavailable" in error for error in report["errors"])


def test_layer_contract_requires_exact_base_diff_id_prefix() -> None:
    base = {
        "reference": "base",
        "image_id": "sha256:base",
        "rootfs_diff_ids": ["one", "two"],
    }
    child = {"rootfs_diff_ids": ["one", "two", "three"]}
    wrong = {"rootfs_diff_ids": ["one", "different", "three"]}

    assert image_contract._layer_contract(child, base) == {
        "base_reference": "base",
        "base_image_id": "sha256:base",
        "prefix_match": True,
        "additional_layer_count": 1,
    }
    assert image_contract._layer_contract(wrong, base)["prefix_match"] is False


def test_validate_keeps_probe_and_layer_failures_in_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identities = {
        "candidate": {
            "reference": "candidate",
            "image_id": "sha256:candidate",
            "os": "linux",
            "architecture": "amd64",
            "rootfs_diff_ids": ["wrong", "child"],
        },
        "base": {
            "reference": "base",
            "image_id": "sha256:base",
            "os": "linux",
            "architecture": "amd64",
            "rootfs_diff_ids": ["base"],
        },
    }
    monkeypatch.setattr(image_contract, "load_contract", lambda *_args: {})
    monkeypatch.setattr(image_contract, "_image_identity", identities.__getitem__)
    monkeypatch.setattr(
        image_contract,
        "_probe_image",
        lambda *_args: {"errors": ["missing command"]},
    )
    contract = tmp_path / "contract.toml"
    contract.write_text("schema = 1\n", encoding="utf-8")

    evidence = image_contract.validate("candidate", "riscv", contract, "base")

    assert evidence["errors"] == [
        "missing command",
        "derived image RootFS layers do not prefix-match the standard image",
    ]
