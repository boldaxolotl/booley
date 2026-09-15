"""Regression gates for the invalid stimuli and wrong verdicts in the sealed run."""

import hashlib
import json
import struct
import subprocess
from pathlib import Path

import pytest
from qa.scenarios.picorv32.fixture_validation import (
    FixtureError,
    canonical_registration,
    distance_rows,
    git_topology,
    lint_command,
    lint_dedupe,
    lint_waiver,
    oracle,
    required_subjects,
    same_bwave_mode,
    spike_elf,
    stealth_native,
    vivado_executable,
    vivado_implementation,
)

FIXTURES = Path(__file__).resolve().parents[2] / "qa/scenarios/picorv32/fixtures"


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, timeout=10)


def _checkout(path: Path) -> None:
    _git("init", "-q", str(path))
    _git("-C", str(path), "-c", "user.name=QA", "-c", "user.email=qa@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "fixture")


def test_outer_marker_is_required_before_inventory_import(tmp_path):
    root = tmp_path / "fixture"
    inner = root / ".booley_project"
    _checkout(inner)
    with pytest.raises(FixtureError, match="outer checkout"):
        git_topology(root, "inventory")
    _checkout(root)
    assert git_topology(root, "inventory")["mode"] == "inventory"
    assert git_topology(root, "ticket")["mode"] == "ticket"


def test_synth_requires_linked_nested_worktree_and_ticket_requires_standalone(tmp_path):
    root = tmp_path / "outer"
    _checkout(root)
    source = tmp_path / "nested-source"
    _checkout(source)
    _git("-C", str(source), "worktree", "add", "--detach", str(root / ".booley_project"))
    assert git_topology(root, "synth")["mode"] == "synth"
    with pytest.raises(FixtureError, match="standalone"):
        git_topology(root, "ticket")
    with pytest.raises(FixtureError, match="destination ref"):
        git_topology(root, "synth", inner_ref="missing-ref")


def _elf32(path: Path, address: int) -> None:
    data = bytearray(128)
    data[:16] = b"\x7fELF\x01\x01\x01" + bytes(9)
    struct.pack_into("<I", data, 28, 52)
    struct.pack_into("<H", data, 42, 32)
    struct.pack_into("<H", data, 44, 1)
    struct.pack_into("<IIIIIIII", data, 52, 1, 0, address, address, 4, 4096, 5, 4096)
    path.write_bytes(data)


def test_spike_rejects_old_below_ram_segment_and_accepts_corrected(tmp_path):
    elf = tmp_path / "probe.elf"
    _elf32(elf, 0x7FFFF000)
    with pytest.raises(FixtureError, match="outside Spike RAM"):
        spike_elf(elf, 0x80000000, 0x80020000)
    _elf32(elf, 0x80010000)
    assert spike_elf(elf, 0x80000000, 0x80020000)["segments"][0]["start"] == 0x80010000
    assert "0x80010000" in (FIXTURES / "riscv/spike-probe.ld").read_text()


def _lint_report(rule: str, target: str) -> dict:
    return {"warnings": [{"rule": rule, "file": "/work/qa_lint_fixture.sv",
                          "line": 2, "message": "width mismatch"}],
            "target_results": [{"target": target}]}


def test_dedupe_requires_same_real_warning_in_both_targets():
    first = _lint_report("WIDTHTRUNC", "a")
    old_invalid = {"warnings": [], "target_results": [{"target": "b"}]}
    combined = {"warnings": first["warnings"],
                "target_results": [{"target": "a"}, {"target": "b"}]}
    with pytest.raises(FixtureError, match="no common real warning"):
        lint_dedupe(first, old_invalid, combined)
    assert lint_dedupe(first, _lint_report("WIDTHTRUNC", "b"), combined)["targets"] == ["a", "b"]
    combined["warnings"] *= 2
    with pytest.raises(FixtureError, match="duplicate"):
        lint_dedupe(first, _lint_report("WIDTHTRUNC", "b"), combined)


def test_native_waiver_only_removes_intended_warning():
    before = {"warnings": [{"rule": "WIDTHTRUNC"}, {"rule": "CONTROL"}]}
    after = {"warnings": [{"rule": "CONTROL"}]}
    assert lint_waiver(before, after, "WIDTHTRUNC", "CONTROL")["preserved"] == "CONTROL"
    with pytest.raises(FixtureError, match="control warning"):
        lint_waiver(before, {"warnings": []}, "WIDTHTRUNC", "CONTROL")
    assert (FIXTURES / "lint/verilator-waiver.vlt").read_text().startswith("`verilator_config")
    assert (FIXTURES / "lint/verible-waiver.txt").read_text() == (
        'waive --rule=no-trailing-spaces --location=".*qa_lint_fixture\\.sv"\n'
    )
    assert lint_waiver({"warnings": [{"rule": "no-trailing-spaces"}]},
                       {"warnings": []}, "no-trailing-spaces")["suppressed"] == "no-trailing-spaces"


def test_verible_renderer_preserves_intended_trailing_space_bytes(tmp_path):
    from qa.scenarios.picorv32.fixtures.lint.render_verible import render

    path = tmp_path / "qa_lint_fixture.sv"
    render(path)
    assert path.read_bytes() == b"module qa_lint_fixture;\n  logic clk;  \nendmodule\n"


def test_lint_command_rejects_missing_executable():
    with pytest.raises(FixtureError, match="omits booley"):
        lint_command(["booley", "session", "enter", "--", "lint", "--target", "a"])
    assert lint_command(["booley", "session", "enter", "--", "booley", "lint"])


def test_mount_probe_discovers_release_layout(tmp_path):
    source = tmp_path / "registration"
    mount = tmp_path / "mount"
    for root in (source, mount):
        command = root / "Vivado/bin/vivado"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
    assert vivado_executable(mount, source)["mount_executable"].endswith("Vivado/bin/vivado")
    (mount / "Vivado/bin/vivado").unlink()
    with pytest.raises(FixtureError, match="mounted Vivado executable missing"):
        vivado_executable(mount, source)


def test_exact_result_oracles_reject_wrong_output(tmp_path):
    assert required_subjects({"isa.pdf": "Unprivileged and Privileged ISA; Debug"},
                             ["Unprivileged", "Privileged", "Debug"])["files"] == 1
    with pytest.raises(FixtureError, match="subjects missing"):
        required_subjects({"isa.pdf": "Unprivileged ISA"}, ["Debug"])
    with pytest.raises(FixtureError, match="start/end relation"):
        distance_rows("@ 8297 -> @ 8303 d=5", [5])
    with pytest.raises(FixtureError, match="expected sequence"):
        distance_rows("@ 8297 -> @ 8303 d=6", [7])
    assert distance_rows("@ 8297 -> @ 8303 d=6\n@ 8303 -> @ 8310 d=7", [6, 7])["rows"] == 2
    assert stealth_native(["rtl/top.v", ".booley/x", "rtl/.hidden.v"])["native_count"] == 1
    child = {"trace": "t", "argv": ["wave"], "signals": ["x"], "clock": None,
             "reset": None, "sampling": "default"}
    replay = dict(child, sampling="explicit")
    with pytest.raises(FixtureError, match="replay differs"):
        same_bwave_mode(child, replay)
    assert same_bwave_mode(child, child)["exact_replay"]
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    assert canonical_registration(alias, source)["canonical_source"] == str(source)
    assert oracle({"kind": "verdict", "declared_expected": "denied", "observed": "pass"})["matches"] is False


def test_vivado_implementation_requires_fresh_declared_artifacts_only(tmp_path):
    report = {"exit_code": 0, "detail": {"implementation": {"grade": "pass", "passed": True},
                                         "cache": {"target": {"cached": False}}}}
    report["detail"]["implementation"]["results"] = {"target": {"status": {"passed": True}}}
    artifacts = {name: {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest(),
                        "mtime_ns": 2} for name in (
        "routed-checkpoint.dcp", "routed-timing.rpt", "routed-utilization.rpt")}
    for name in artifacts:
        (tmp_path / name).write_bytes(b"x")
    assert vivado_implementation(report, artifacts, {}, tmp_path)["grade"] == "pass"
    with pytest.raises(FixtureError, match="not fresh"):
        vivado_implementation(report, artifacts, {"routed-checkpoint.dcp": {"mtime_ns": 2}},
                              tmp_path)
    with pytest.raises(FixtureError, match="missing declared"):
        vivado_implementation(report, {}, {}, tmp_path)


def test_wrong_verdict_cli_exits_nonzero_and_retains_both_values(tmp_path):
    input_file = tmp_path / "verdict.json"
    input_file.write_text(json.dumps({"kind": "verdict", "declared_expected": "denied",
                                      "observed": "pass"}))
    script = FIXTURES.parent / "fixture_validation.py"
    result = subprocess.run(["python3", str(script), "oracle", str(input_file)],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"declared_expected": "denied",
                                         "observed": "pass", "matches": False}
