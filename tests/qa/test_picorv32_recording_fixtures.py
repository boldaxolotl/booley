"""Regression gates for the invalid stimuli and wrong verdicts in the sealed run."""

import copy
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
    doctor_warning_comparison,
    git_topology,
    interactive_repair,
    lint_command,
    lint_dedupe,
    lint_waiver,
    oracle,
    packet_input,
    required_subjects,
    same_bwave_mode,
    spike_elf,
    stealth_native,
    synth_baseline,
    ticket_routing,
    vivado_executable,
    vivado_implementation,
)

FIXTURES = Path(__file__).resolve().parents[2] / "qa/scenarios/picorv32/fixtures"
OUTER_REF = "refs/heads/outer"
INNER_REF = "refs/heads/project"


def _ticket_packet() -> bytes:
    return (
        b"```markdown\n---\nsummary: exact\ntype: feature\nbranch: outer\n"
        b"project_destination_ref: refs/heads/project\nscope: [README.md]\n"
        b"dependencies: []\npriority: medium\non_success: [review]\n"
        b"CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: clean}}}\n---\nbody\n```\n"
    )


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, timeout=10)


def _checkout(path: Path) -> None:
    _git("init", "-q", str(path))
    _git(
        "-C",
        str(path),
        "-c",
        "user.name=QA",
        "-c",
        "user.email=qa@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "fixture",
    )


def test_outer_marker_is_required_before_inventory_import(tmp_path):
    root = tmp_path / "fixture"
    inner = root / ".booley_project"
    _checkout(inner)
    with pytest.raises(FixtureError, match="outer checkout"):
        git_topology(root, "inventory")
    _checkout(root)
    assert git_topology(root, "inventory")["mode"] == "inventory"
    with pytest.raises(FixtureError, match="requires explicit --outer-ref and --inner-ref"):
        git_topology(root, "ticket")


def test_synth_requires_linked_nested_worktree_and_ticket_requires_standalone(tmp_path):
    root = tmp_path / "outer"
    _checkout(root)
    source = tmp_path / "nested-source"
    _checkout(source)
    _git("-C", str(root), "branch", "outer-release")
    _git("-C", str(source), "branch", "project-data")
    _git("-C", str(source), "worktree", "add", "--detach", str(root / ".booley_project"))
    assert git_topology(root, "synth")["mode"] == "synth"
    with pytest.raises(FixtureError, match="standalone"):
        git_topology(
            root,
            "ticket",
            outer_ref="refs/heads/outer-release",
            inner_ref="refs/heads/project-data",
        )
    with pytest.raises(FixtureError, match="destination ref"):
        git_topology(root, "synth", inner_ref="missing-ref")


def test_ticket_topology_requires_full_local_branch_refs(tmp_path):
    root = tmp_path / "outer"
    inner = root / ".booley_project"
    _checkout(root)
    _checkout(inner)

    with pytest.raises(FixtureError, match="outer ref must be a full refs/heads"):
        git_topology(root, "ticket", outer_ref="main", inner_ref="refs/heads/main")
    with pytest.raises(FixtureError, match="inner ref must be a full refs/heads"):
        git_topology(root, "ticket", outer_ref="refs/heads/main", inner_ref="main")
    result = subprocess.run(
        [
            "python3",
            str(FIXTURES.parent / "fixture_validation.py"),
            "git-topology",
            str(root),
            "ticket",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "requires explicit --outer-ref and --inner-ref" in result.stderr


def test_packet_input_stages_exact_bytes_and_detects_drift(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "resolved.md"
    packet = _ticket_packet()
    source.write_bytes(packet)
    staged = project / "tmp/qa-inputs/run-1/ticket-2/packet.md"

    result = packet_input(project, source, staged, outer_ref=OUTER_REF, inner_ref=INNER_REF)

    assert staged.read_bytes() == packet
    assert result["byte_count"] == len(packet)
    assert result["sha256"] == hashlib.sha256(packet).hexdigest()
    staged.write_bytes(b"changed\n")
    with pytest.raises(FixtureError, match="staged packet differs"):
        packet_input(
            project,
            source,
            staged,
            verify_only=True,
            outer_ref=OUTER_REF,
            inner_ref=INNER_REF,
        )


def test_packet_input_replacement_failure_preserves_existing_packet(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "resolved.md"
    source.write_bytes(_ticket_packet())
    staged = project / "tmp/qa-inputs/packet.md"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"previous complete packet\n")
    real_replace = Path.replace

    def fail_publication(path: Path, target: Path) -> Path:
        if target == staged:
            raise OSError("injected publication interruption")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_publication)

    with pytest.raises(OSError, match="injected publication interruption"):
        packet_input(project, source, staged, outer_ref=OUTER_REF, inner_ref=INNER_REF)

    assert staged.read_bytes() == b"previous complete packet\n"
    assert list(staged.parent.glob(".packet.md.*.tmp")) == []


def test_packet_input_staging_keeps_initialized_project_clean(tmp_path):
    project = tmp_path / "project"
    _checkout(project)
    (project / ".gitignore").write_text("tmp/\n")
    _git("-C", str(project), "add", ".gitignore")
    _git(
        "-C",
        str(project),
        "-c",
        "user.name=QA",
        "-c",
        "user.email=qa@example.invalid",
        "commit",
        "-q",
        "-m",
        "ignore runtime",
    )
    source = tmp_path / "resolved.md"
    source.write_bytes(_ticket_packet())

    packet_input(
        project,
        source,
        project / "tmp/qa-inputs/run/step/packet.md",
        outer_ref=OUTER_REF,
        inner_ref=INNER_REF,
    )

    status = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert status.stdout == ""


def test_packet_input_rejects_paths_outside_project_and_unresolved_tokens(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "resolved.md"
    source.write_text("{{ unresolved }}\n")

    with pytest.raises(FixtureError, match="unresolved substitution"):
        packet_input(
            project,
            source,
            project / "tmp/qa-inputs/packet.md",
            outer_ref=OUTER_REF,
            inner_ref=INNER_REF,
        )
    source.write_text("resolved\n")
    with pytest.raises(FixtureError, match="beneath the Project directory"):
        packet_input(
            project,
            source,
            tmp_path / "outside.md",
            outer_ref=OUTER_REF,
            inner_ref=INNER_REF,
        )


def test_ticket_packet_cli_requires_both_routing_refs(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "resolved.md"
    source.write_bytes(_ticket_packet())
    staged = project / "tmp/qa-inputs/packet.md"

    result = subprocess.run(
        [
            "python3",
            str(FIXTURES.parent / "fixture_validation.py"),
            "ticket-packet",
            str(project),
            str(source),
            str(staged),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "--outer-ref" in result.stderr
    assert "--inner-ref" in result.stderr


def test_packet_input_rejects_retired_vocabulary_and_wrong_routing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "resolved.md"
    staged = project / "tmp/qa-inputs/packet.md"
    packet = (
        "```markdown\n---\nsummary: exact\ntype: feature\nbranch: outer\n"
        "project_destination_ref: refs/heads/project\nscope: [README.md]\n"
        "dependencies: []\npriority: medium\non_success: [review]\n"
        "CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: clean}}}\n---\nbody\n```\n"
    )
    source.write_text(packet)

    result = packet_input(
        project,
        source,
        staged,
        outer_ref="refs/heads/outer",
        inner_ref="refs/heads/project",
    )
    assert result["branch"] == "outer"
    assert result["project_destination_ref"] == "refs/heads/project"

    source.write_text(packet.replace("branch: outer", "branch: project"))
    with pytest.raises(FixtureError, match="branch does not match"):
        packet_input(
            project,
            source,
            staged,
            outer_ref="refs/heads/outer",
            inner_ref="refs/heads/project",
        )
    source.write_text(
        packet.replace("CRITERIA_MANDATORY:", "target_plan: []\nCRITERIA_MANDATORY:")
    )
    with pytest.raises(FixtureError, match="retired Ticket vocabulary"):
        packet_input(
            project,
            source,
            staged,
            outer_ref="refs/heads/outer",
            inner_ref="refs/heads/project",
        )


def test_packet_input_accepts_crlf_packet_bytes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "resolved.md"
    packet = _ticket_packet().replace(b"\n", b"\r\n")
    source.write_bytes(packet)
    staged = project / "tmp/qa-inputs/packet.md"

    result = packet_input(
        project,
        source,
        staged,
        outer_ref=OUTER_REF,
        inner_ref=INNER_REF,
    )

    assert staged.read_bytes() == packet
    assert result["sha256"] == hashlib.sha256(packet).hexdigest()


def test_ticket_routing_proves_distinct_mapping_and_rejects_swap(tmp_path):
    root = tmp_path / "outer"
    inner = root / ".booley_project"
    _checkout(root)
    _checkout(inner)
    _git("-C", str(root), "branch", "outer-release")
    _git("-C", str(root), "branch", "project-data")
    _git("-C", str(inner), "branch", "project-data")
    _git("-C", str(inner), "branch", "outer-release")
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\nbranch: outer-release\n"
        "project_destination_ref: refs/heads/project-data\n---\n\n## Description\n"
    )

    result = ticket_routing(
        root,
        ticket,
        "refs/heads/outer-release",
        "refs/heads/project-data",
    )

    assert result["branch"] == "outer-release"
    assert result["project_destination_ref"] == "refs/heads/project-data"
    assert len(result["outer_commit"]) == 40
    assert len(result["project_commit"]) == 40
    with pytest.raises(FixtureError, match="branch does not match"):
        ticket_routing(
            root,
            ticket,
            "refs/heads/project-data",
            "refs/heads/outer-release",
        )


def test_ticket_routing_rejects_collapsed_mapping(tmp_path):
    root = tmp_path / "outer"
    inner = root / ".booley_project"
    _checkout(root)
    _checkout(inner)
    ticket = tmp_path / "ticket.md"
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    ticket.write_text(
        f"---\nbranch: {branch}\n"
        f"project_destination_ref: refs/heads/{branch}\n---\n\n## Description\n"
    )

    with pytest.raises(FixtureError, match="must be distinct"):
        ticket_routing(
            root,
            ticket,
            f"refs/heads/{branch}",
            f"refs/heads/{branch}",
        )


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


def test_spike_rejects_truncated_identification_header(tmp_path):
    elf = tmp_path / "truncated.elf"
    elf.write_bytes(b"\x7fELF\x01")
    with pytest.raises(FixtureError, match="truncated ELF identification"):
        spike_elf(elf, 0x80000000, 0x80020000)


def _lint_report(rule: str, target: str) -> dict:
    return {
        "warnings": [
            {
                "rule": rule,
                "file": "/work/qa_lint_fixture.sv",
                "line": 2,
                "message": "width mismatch",
            }
        ],
        "target_results": [{"target": target}],
    }


def test_dedupe_requires_same_real_warning_in_both_targets():
    first = _lint_report("WIDTHTRUNC", "a")
    old_invalid = {"warnings": [], "target_results": [{"target": "b"}]}
    combined = {
        "warnings": first["warnings"],
        "target_results": [{"target": "a"}, {"target": "b"}],
    }
    with pytest.raises(FixtureError, match="no common real warning"):
        lint_dedupe(first, old_invalid, combined)
    assert lint_dedupe(first, _lint_report("WIDTHTRUNC", "b"), combined)["targets"] == ["a", "b"]
    with pytest.raises(FixtureError, match="two distinct Targets"):
        lint_dedupe(first, first, combined)
    combined["warnings"] *= 2
    with pytest.raises(FixtureError, match="duplicate"):
        lint_dedupe(first, _lint_report("WIDTHTRUNC", "b"), combined)


def test_native_waiver_only_removes_intended_warning():
    intended = {"rule": "WIDTHTRUNC", "file": "/work/a.sv", "line": 2, "message": "width mismatch"}
    control = {"rule": "CONTROL", "file": "/work/a.sv", "line": 3, "message": "control warning"}
    before = {"warnings": [intended, control]}
    after = {"warnings": [control]}
    assert lint_waiver(before, after, "WIDTHTRUNC", "CONTROL")["preserved"] == "CONTROL"
    with pytest.raises(FixtureError, match="control warning"):
        lint_waiver(before, {"warnings": []}, "WIDTHTRUNC", "CONTROL")
    assert (FIXTURES / "lint/verilator-waiver.vlt").read_text().startswith("`verilator_config")
    assert (FIXTURES / "lint/verible-waiver.txt").read_text() == (
        'waive --rule=no-trailing-spaces --location=".*qa_lint_fixture\\.sv"\n'
    )
    verible = {
        "rule": "no-trailing-spaces",
        "file": "/work/a.sv",
        "line": 2,
        "message": "trailing whitespace",
    }
    assert (
        lint_waiver({"warnings": [verible]}, {"warnings": []}, "no-trailing-spaces")["suppressed"]
        == "no-trailing-spaces"
    )
    unrelated_same_rule = dict(intended, file="/work/b.sv", line=8)
    with pytest.raises(FixtureError, match="exactly one intended warning"):
        lint_waiver(
            {"warnings": [intended, unrelated_same_rule, control]},
            {"warnings": [control]},
            "WIDTHTRUNC",
            "CONTROL",
        )


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
    executable = Path(vivado_executable(mount, source)["mount_executable"])
    assert executable.parts[-3:] == ("Vivado", "bin", "vivado")
    (mount / "Vivado/bin/vivado").unlink()
    with pytest.raises(FixtureError, match="mounted Vivado executable missing"):
        vivado_executable(mount, source)


def test_exact_result_oracles_reject_wrong_output(tmp_path):
    assert (
        required_subjects(
            {"isa.pdf": "Unprivileged and Privileged ISA; Debug"},
            ["Unprivileged", "Privileged", "Debug"],
        )["files"]
        == 1
    )
    with pytest.raises(FixtureError, match="subjects missing"):
        required_subjects({"isa.pdf": "Unprivileged ISA"}, ["Debug"])
    with pytest.raises(FixtureError, match="start/end relation"):
        distance_rows("@ 8297 -> @ 8303 d=5", [5])
    with pytest.raises(FixtureError, match="expected sequence"):
        distance_rows("@ 8297 -> @ 8303 d=6", [7])
    assert distance_rows("@ 8297 -> @ 8303 d=6\n@ 8303 -> @ 8310 d=7", [6, 7])["rows"] == 2
    assert stealth_native(["rtl/top.v", ".booley/x", "rtl/.hidden.v"])["native_count"] == 1
    with pytest.raises(FixtureError, match="list of nonempty strings"):
        oracle({"kind": "stealth-native", "paths": "rtl/top.v"})
    child = {
        "trace": "t",
        "argv": ["wave"],
        "signals": ["x"],
        "clock": None,
        "reset": None,
        "sampling": "default",
    }
    replay = dict(child, sampling="explicit")
    with pytest.raises(FixtureError, match="replay differs"):
        same_bwave_mode(child, replay)
    assert same_bwave_mode(child, child)["exact_replay"]
    with pytest.raises(FixtureError, match="incomplete"):
        same_bwave_mode({}, {})
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    assert canonical_registration(alias, source)["canonical_source"] == str(source)
    assert (
        oracle({"kind": "verdict", "declared_expected": "denied", "observed": "pass"})["matches"]
        is False
    )


def test_synth_baseline_requires_successful_numeric_comparison_and_identities():
    expected = {
        "candidate_target": "synth_core",
        "baseline_target": "synth_core",
        "candidate_identity": "booley::candidate:0#synth_core",
        "baseline_identity": "booley::baseline:0#synth_core",
        "candidate_revision": "b" * 40,
        "baseline_revision": "a" * 40,
        "delta_pct": 0.0,
        "timing_delta_pct": -1.5,
    }
    summary = {
        **expected,
        "flow_exit": 0,
        "infra_error": None,
        "baseline": {"ref": "a" * 7, "area_kge": 42},
        "delta_pct": 0.0,
        "timing_delta_pct": -1.5,
    }
    summary.pop("candidate_revision")
    summary.pop("baseline_revision")
    report = {
        "detail": {
            "implementation": {
                "results": {
                    "synth_core": {
                        "identity": {"target_identity": expected["candidate_identity"]},
                        "provenance": {
                            "producer": {"source_revision": expected["candidate_revision"]}
                        },
                        "comparison": {
                            "basis_valid": True,
                            "basis_errors": [],
                            "candidate_target_identity": expected["candidate_identity"],
                            "baseline_target_identity": expected["baseline_identity"],
                            "deltas": {
                                "area_kge": {"delta_pct": 0.0},
                                "wns_ns": {"delta_pct": -1.5},
                            },
                            "baseline": {
                                "provenance": {
                                    "producer": {"source_revision": expected["baseline_revision"]}
                                }
                            },
                        },
                    }
                }
            }
        }
    }
    assert synth_baseline(summary, report, expected)["baseline_revision"] == "a" * 40
    wrong = dict(summary, delta_pct="0.0")
    with pytest.raises(FixtureError, match="numeric delta_pct"):
        synth_baseline(wrong, report, expected)
    wrong = dict(summary, delta_pct=3.0)
    with pytest.raises(FixtureError, match="declared numeric result"):
        synth_baseline(wrong, report, expected)
    wrong = dict(summary, candidate_identity="booley::wrong:0#synth_core")
    with pytest.raises(FixtureError, match="candidate_identity differs"):
        synth_baseline(wrong, report, expected)
    wrong_report = copy.deepcopy(report)
    wrong_report["detail"]["implementation"]["results"]["synth_core"]["provenance"]["producer"][
        "source_revision"
    ] = "c" * 40
    with pytest.raises(FixtureError, match="candidate revision differs"):
        synth_baseline(summary, wrong_report, expected)


def test_doctor_warning_comparison_ignores_generated_instance_names():
    def report(instance: str, *, truncated: bool = False) -> dict:
        return {
            "warning_summary": {
                "total_warnings": 2,
                "representatives": [
                    {
                        "tool": "openroad",
                        "code": "STA-0349",
                        "category": "constraint",
                        "count": 1,
                        "message": f"[WARNING STA-0349] instance {instance} missing clock.",
                        "truncated": truncated,
                    },
                    {
                        "tool": "yosys",
                        "code": None,
                        "category": "other",
                        "count": 1,
                        "message": "Warning: unused wire.",
                    },
                ],
            }
        }

    result = doctor_warning_comparison(
        report("_20155c71ac5a0000_p_Instance"),
        report("_20159d53f5580000_p_Instance"),
    )
    assert result["stable_warning_count"] == 2

    truncated_pre = report("_deadbeef_p_In", truncated=True)
    truncated_final = report("_01234567_p_Inst", truncated=True)
    for candidate, instance in (
        (truncated_pre, "_deadbeef_p_In"),
        (truncated_final, "_01234567_p_Inst"),
    ):
        candidate["warning_summary"]["representatives"][0]["message"] = (
            f"[WARNING STA-0349] instance {instance}…"
        )
    assert doctor_warning_comparison(truncated_pre, truncated_final)["stable_warning_count"] == 2

    changed = report("_20159d53f5580000_p_Instance")
    changed["warning_summary"]["representatives"][0]["message"] = (
        "[WARNING STA-0349] instance _20159d53f5580000_p_Instance has no clock."
    )
    with pytest.raises(FixtureError, match="warning signatures differ"):
        doctor_warning_comparison(report("_20155c71ac5a0000_p_Instance"), changed)
    for field, value in (("code", "STA-0350"), ("category", "other"), ("count", 2)):
        changed = report("_20159d53f5580000_p_Instance")
        changed["warning_summary"]["representatives"][0][field] = value
        with pytest.raises(FixtureError, match="warning signatures differ"):
            doctor_warning_comparison(report("_20155c71ac5a0000_p_Instance"), changed)


def test_interactive_repair_rejects_exact_head_restoration():
    baseline = "assign we = (mem_wstrb[0] | mem_wstrb[1]);\n"
    injected = "assign we = (mem_wstrb[0] & mem_wstrb[1]);\n"
    repaired = "assign we = |mem_wstrb;\n"

    faulty = "assign we = (mem_wstrb[0] & mem_wstrb[1]);\n"
    fixed = "assign we = |mem_wstrb;\n"
    assert interactive_repair(baseline, injected, repaired, faulty, fixed)["meaningful_diff"]
    with pytest.raises(FixtureError, match="byte-identical to baseline"):
        interactive_repair(baseline, injected, baseline, faulty, fixed)
    with pytest.raises(FixtureError, match="replace only the declared fault"):
        interactive_repair(
            baseline,
            injected,
            injected.replace(faulty, "") + "// assign we = |mem_wstrb;\n",
            faulty,
            fixed,
        )


def test_vivado_implementation_requires_fresh_declared_artifacts_only(tmp_path):
    report = {
        "exit_code": 0,
        "detail": {
            "implementation": {"grade": "pass", "passed": True},
            "cache": {"target": {"cached": False}},
        },
    }
    report["detail"]["implementation"]["results"] = {"target": {"status": {"passed": True}}}
    artifacts = {
        name: {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest(), "mtime_ns": 2}
        for name in ("routed-checkpoint.dcp", "routed-timing.rpt", "routed-utilization.rpt")
    }
    for name in artifacts:
        (tmp_path / name).write_bytes(b"x")
    assert vivado_implementation(report, artifacts, {}, tmp_path)["grade"] == "pass"
    with pytest.raises(FixtureError, match="not fresh"):
        vivado_implementation(
            report, artifacts, {"routed-checkpoint.dcp": {"mtime_ns": 2}}, tmp_path
        )
    with pytest.raises(FixtureError, match="missing declared"):
        vivado_implementation(report, {}, {}, tmp_path)


def test_wrong_verdict_cli_exits_nonzero_and_retains_both_values(tmp_path):
    input_file = tmp_path / "verdict.json"
    input_file.write_text(
        json.dumps({"kind": "verdict", "declared_expected": "denied", "observed": "pass"})
    )
    script = FIXTURES.parent / "fixture_validation.py"
    result = subprocess.run(
        ["python3", str(script), "oracle", str(input_file)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "declared_expected": "denied",
        "observed": "pass",
        "matches": False,
    }


def test_oracle_cli_rejects_malformed_nested_json_without_traceback(tmp_path):
    input_file = tmp_path / "malformed.json"
    input_file.write_text(
        json.dumps(
            {
                "kind": "vivado-implementation",
                "report": {"exit_code": 0, "detail": []},
                "artifacts": {},
                "before": {},
                "retained_dir": str(tmp_path),
            }
        )
    )
    script = FIXTURES.parent / "fixture_validation.py"
    result = subprocess.run(
        ["python3", str(script), "oracle", str(input_file)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "must be a JSON object" in result.stderr
    assert "Traceback" not in result.stderr
