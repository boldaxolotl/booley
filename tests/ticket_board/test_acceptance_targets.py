"""Acceptance Target behavior exercised through its public validation contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.ticket_board import (
    acceptance_targets,
)
from booley.ticket_board.acceptance_basis import (
    BasisParticipant,
)


def _completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


def _target_input(
    path: str,
    *,
    file_type: str = "systemVerilogSource",
    tags: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(path=path, file_type=file_type, tags=tags)


def test_target_binding_rejects_incomplete_and_noncanonical_values() -> None:
    binding = acceptance_targets.AcceptanceTargetBinding(
        "sim",
        "criteria.mandatory.sim_pass",
        "acme:lib:toy:1#sim",
        "acme:lib:toy:1#sim",
        "sim",
        " sim",
    )

    with pytest.raises(ValueError, match="candidate_selector"):
        binding.validate_persisted()
    with pytest.raises(ValueError, match="full"):
        acceptance_targets.AcceptanceTargetBinding(
            "sim", "sim_pass", "base", "candidate", "base", "candidate"
        ).validate_persisted()


def test_target_binding_accepts_optional_criterion_path() -> None:
    binding = acceptance_targets.AcceptanceTargetBinding(
        "lint",
        "criteria.optional.lint_clean",
        "acme:lib:toy:1#lint",
        "acme:lib:toy:1#lint",
        "lint",
        "lint",
    )

    assert binding.validate_persisted() is binding
    assert binding.criterion_key == "lint_clean"
    assert binding.as_dict()["criterion"] == "criteria.optional.lint_clean"


def test_target_control_helpers_handle_external_and_missing_project_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / "outside.core"
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry, "discover_cores", lambda _root: (outside,)
    )
    monkeypatch.setattr(acceptance_targets.fusesoc_registry, "read_core", lambda _path: {})
    monkeypatch.setattr(
        acceptance_targets.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git"),
    )
    monkeypatch.setattr(
        acceptance_targets,
        "resolve_checkout_project_dir",
        lambda _root: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert acceptance_targets.acceptance_control_paths(tmp_path) == (outside.as_posix(),)


def test_core_referenced_files_ignore_invalid_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "toy.core"
    document = {
        "filesets": {
            "not-a-map": [],
            "no-files": {},
            "mixed": {
                "files": [
                    "rtl/toy.sv",
                    {"constraints/toy.sdc": {"file_type": "SDC"}},
                    {},
                    7,
                ]
            },
        }
    }
    constraint = tmp_path / "constraints/toy.sdc"
    constraint.parent.mkdir()
    constraint.write_text("create_clock -period 10 clk\n", encoding="utf-8")

    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry, "discover_cores", lambda _root: (core,)
    )
    monkeypatch.setattr(acceptance_targets.fusesoc_registry, "read_core", lambda _path: document)
    monkeypatch.setattr(acceptance_targets, "_project_control_files", lambda _root: ())
    monkeypatch.setattr(
        acceptance_targets.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git"),
    )
    paths = acceptance_targets.acceptance_control_paths(tmp_path)
    assert "constraints/toy.sdc" in paths


def test_tracked_gitlinks_reports_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="bad index"),
    )

    with pytest.raises(acceptance_targets.BoundaryError, match="bad index"):
        acceptance_targets.acceptance_control_paths(tmp_path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"target": "lint_a"}, [("lint_a", "lint_a", False)]),
        (
            {
                "targets": [{"baseline": "synth_a", "candidate": "synth_b"}],
                "area_reduce_at_least": "5%",
            },
            [("synth_b", "synth_a", True)],
        ),
        ({"targets": [7]}, []),
        ("invalid", []),
    ],
)
def test_target_value_parser_handles_supported_and_invalid_shapes(
    value: object, expected: list[tuple[str, str, bool]]
) -> None:
    bindings = acceptance_targets.criterion_targets({"mandatory": {"synthesis_ok": value}})
    assert [(item.target, item.baseline, item.relative) for item in bindings] == expected


def test_target_list_parser_handles_coverage_sim_and_invalid_items() -> None:
    value = [
        {"targets": ["cov_a", "cov_b"]},
        {"target": "sim_map"},
        "tb/test.sv @ sim_text @ all @ none -> pass",
        "lint_plain",
        "invalid @ text",
        3,
    ]
    bindings = acceptance_targets.criterion_targets({"mandatory": {"coverage": value}})
    assert [(item.target, item.baseline, item.relative) for item in bindings] == [
        ("cov_a", "cov_a", False),
        ("cov_b", "cov_b", False),
        ("sim_map", "sim_map", False),
        ("sim_text", "sim_text", False),
        ("lint_plain", "lint_plain", False),
    ]


def test_target_list_parser_ignores_invalid_sim_expression() -> None:
    assert (
        acceptance_targets.criterion_targets(
            {"mandatory": {"sim_pass": ["broken -> expression"], "unknown": "target"}}
        )
        == ()
    )


def test_missing_target_sources_normalizes_relative_and_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "rtl/existing.sv"
    existing.parent.mkdir()
    existing.write_text("module existing; endmodule\n", encoding="utf-8")
    absolute = tmp_path / "absolute.sv"
    monkeypatch.setattr(
        acceptance_targets,
        "inspect_target_selector",
        lambda *_args: SimpleNamespace(
            inputs=(
                _target_input("rtl/existing.sv"),
                _target_input("rtl/missing.sv"),
                _target_input(str(absolute)),
                _target_input("rtl/missing.sv"),
            )
        ),
    )
    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda *_args: SimpleNamespace(flow="sim", eda_tool="iverilog"),
    )
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: True)
    errors = acceptance_targets.validate_criterion_targets(
        {"criteria": {"mandatory": {"sim_pass": ["sim"]}}, "scope": []}, tmp_path
    )
    assert str(absolute) in errors[0]
    assert "rtl/missing.sv" in errors[0]


def test_criterion_targets_ignore_unknown_sections_and_keys() -> None:
    criteria = {
        "mandatory": {"review_rtl_bugs": True, "lint_clean": ["lint"]},
        "optional": "invalid",
    }

    assert acceptance_targets.criterion_targets(None) == ()
    (binding,) = acceptance_targets.criterion_targets(criteria)
    assert binding.target == "lint"
    assert binding.flow == "lint"


def test_coverage_suite_validation_reports_registry_and_selection_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    criteria = {"mandatory": {"coverage": [{"targets": ["sim"], "tests": ["missing"]}]}}
    monkeypatch.setattr(
        acceptance_targets,
        "resolve_checkout_project_dir",
        lambda _root: tmp_path / ".booley_project",
    )
    monkeypatch.setattr(acceptance_targets, "_validate_binding", lambda *_args: [])
    assert (
        "cannot validate registered tests"
        in acceptance_targets.validate_criterion_targets({"criteria": criteria}, tmp_path)[0]
    )
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "tests.toml").write_text("[targets.sim]\ntests = ['known']\n", encoding="utf-8")
    assert acceptance_targets.validate_criterion_targets({"criteria": criteria}, tmp_path) == [
        "criteria.mandatory.coverage: target 'sim' has unregistered tests: missing"
    ]
    assert acceptance_targets.validate_criterion_targets({}, tmp_path) == []


@pytest.mark.parametrize(
    ("scope", "path", "expected"),
    [
        (None, "rtl/new.sv", False),
        ([7, "rtl/new.sv"], "rtl/new.sv", False),
        (["rtl/new.sv [new]"], "./rtl/new.sv", True),
        (["rtl/new [new]"], "rtl/new/child.sv", True),
        (["rtl/*.sv [new]"], "rtl/new.sv", True),
    ],
)
def test_new_scope_matching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: object,
    path: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda *_args: SimpleNamespace(flow="sim", eda_tool="iverilog"),
    )
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: True)
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda *_args: (_target_input(path),),
    )
    errors = acceptance_targets.validate_criterion_targets(
        {"criteria": {"mandatory": {"sim_pass": ["sim"]}}, "scope": scope}, tmp_path
    )
    assert (not errors) is expected


def test_validate_changed_targets_handles_duplicate_missing_and_resolvable_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = {
        "new": (_target_input("rtl/new.sv"),),
        "undeclared": (_target_input("rtl/nope.sv"),),
        "ready": (),
    }
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda _root, target: missing[target],
    )
    monkeypatch.setattr(
        acceptance_targets,
        "_dry_resolve_binding",
        lambda binding, _root, build: [f"resolved {binding.target} in {build.name}"],
    )

    errors = acceptance_targets.validate_acceptance_targets(
        {"scope": ["rtl/new.sv [new]"]},
        tmp_path,
        tmp_path / "build",
        changed_targets=["new", "undeclared", "ready", "ready"],
    )

    assert errors[0].startswith("changed Target 'undeclared'")
    assert errors[1] == "resolved ready in ready"


@pytest.mark.parametrize(
    ("path", "file_type", "tags"),
    [
        ("constraints/new.sdc", "SDC", ()),
        ("constraints/new.xdc", "xdc", ("tb",)),
        ("firmware/new.hex", "user", ("tb",)),
        ("hooks/new.tcl", "tclSource", ("tb",)),
    ],
)
def test_scope_new_cannot_defer_missing_non_hdl_target_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    file_type: str,
    tags: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda *_args: SimpleNamespace(flow="sim", eda_tool="iverilog"),
    )
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: True)
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda *_args: (_target_input(path, file_type=file_type, tags=tags),),
    )

    errors = acceptance_targets.validate_criterion_targets(
        {
            "criteria": {"mandatory": {"sim_pass": ["sim"]}},
            "scope": [f"{path} [new]"],
        },
        tmp_path,
    )

    assert len(errors) == 1
    assert "missing non-RTL/TB input(s)" in errors[0]
    assert f"file_type={file_type!r}" in errors[0]


@pytest.mark.parametrize(
    ("file_type", "tags"),
    [
        ("verilogSource-2005", ()),
        ("systemVerilogSource", ()),
        ("vhdlSource-2008", ()),
        ("cSource", ("tb",)),
        ("cppSource", ("tb",)),
    ],
)
def test_scope_new_can_defer_missing_rtl_or_testbench_target_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_type: str,
    tags: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda *_args: SimpleNamespace(flow="sim", eda_tool="iverilog"),
    )
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: True)
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda *_args: (_target_input("sources/new", file_type=file_type, tags=tags),),
    )

    errors = acceptance_targets.validate_criterion_targets(
        {
            "criteria": {"mandatory": {"sim_pass": ["sim"]}},
            "scope": ["sources/new [new]"],
        },
        tmp_path,
    )

    assert errors == []


def test_scope_new_can_defer_missing_cocotb_python_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda *_args: SimpleNamespace(flow="sim", eda_tool="iverilog"),
    )
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: True)
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda *_args: (_target_input("tb/test_new.py", file_type="user", tags=("tb",)),),
    )

    errors = acceptance_targets.validate_criterion_targets(
        {
            "criteria": {"mandatory": {"sim_pass": ["sim"]}},
            "scope": ["tb/test_new.py [new]"],
        },
        tmp_path,
    )

    assert errors == []


def test_changed_target_cannot_defer_missing_non_hdl_target_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda *_args: (_target_input("constraints/new.sdc", file_type="SDC"),),
    )

    errors = acceptance_targets.validate_acceptance_targets(
        {"scope": ["constraints/new.sdc [new]"]},
        tmp_path,
        tmp_path / "build",
        changed_targets=["future"],
    )

    assert len(errors) == 1
    assert "changed Target 'future' has missing non-RTL/TB input(s)" in errors[0]


def test_validate_acceptance_targets_stops_after_binding_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets, "validate_criterion_targets", lambda *_args: ["bad binding"]
    )
    assert acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build") == [
        "bad binding"
    ]


def test_required_targets_promote_baselines_and_resolvable_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_sources",
        lambda _root, target: ["new.sv"] if target == "candidate" else [],
    )
    resolved: list[str] = []
    monkeypatch.setattr(
        acceptance_targets,
        "_dry_resolve_binding",
        lambda _binding, _root, _build, *, target=None: resolved.append(target or "") or [],
    )
    monkeypatch.setattr(acceptance_targets, "validate_criterion_targets", lambda *_args: [])
    fields = {
        "criteria": {
            "mandatory": {
                "synthesis_ok": {
                    "targets": [{"baseline": "baseline", "candidate": "candidate"}],
                    "area_reduce_at_least": "5%",
                }
            },
            "optional": {"synthesis_ok": {"target": "candidate"}},
        }
    }
    assert (
        acceptance_targets.validate_acceptance_targets(fields, tmp_path, tmp_path / "build") == []
    )
    assert resolved == ["baseline"]


def test_validate_required_targets_skips_deferred_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(acceptance_targets, "validate_criterion_targets", lambda *_args: [])
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_sources",
        lambda _root, target: ["new.sv"] if target == "deferred" else [],
    )
    monkeypatch.setattr(
        acceptance_targets,
        "_dry_resolve_binding",
        lambda _binding, _root, _build, *, target=None: calls.append(target or "") or ["bad"],
    )
    errors = acceptance_targets.validate_acceptance_targets(
        {"criteria": {"mandatory": {"lint_clean": ["deferred", "required"]}}},
        tmp_path,
        tmp_path / "build",
    )
    assert errors == ["bad"]
    assert calls == ["required"]


def test_comparison_basis_reports_resolution_and_recipe_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.CriterionTarget(
        "mandatory", "synthesis_ok", "candidate", "synth", True, "baseline"
    )
    monkeypatch.setattr(acceptance_targets, "validate_criterion_targets", lambda *_args: [])
    monkeypatch.setattr(acceptance_targets, "criterion_targets", lambda *_args: (binding,))
    monkeypatch.setattr(acceptance_targets, "_missing_target_sources", lambda *_args: [])
    monkeypatch.setattr(acceptance_targets, "_dry_resolve_binding", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        acceptance_targets,
        "_comparison_snapshots",
        lambda *_args: (_ for _ in ()).throw(acceptance_targets.BoundaryError("broken")),
    )
    assert (
        "cannot compare"
        in acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build")[0]
    )
    monkeypatch.setattr(acceptance_targets, "_comparison_snapshots", lambda *_args: None)
    assert acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build") == []

    monkeypatch.setattr(
        acceptance_targets,
        "_comparison_snapshots",
        lambda *_args: ({"tool": "a"}, {"tool": "b"}),
    )
    monkeypatch.setattr(
        "booley.flows.recipe_evidence.implementation_comparison_basis", lambda value: value
    )
    monkeypatch.setattr(
        "booley.flows.recipe_evidence.recipe_changes",
        lambda _left, _right: [{"path": "tool"}],
    )
    assert (
        "incompatible measurement bases (tool)"
        in acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build")[0]
    )


def test_comparison_snapshots_dispatch_by_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda target, **_kwargs: SimpleNamespace(name=target),
    )
    monkeypatch.setattr("booley.flows.synth.recipe.default_recipe_args", SimpleNamespace)
    monkeypatch.setattr(
        "booley.flows.synth.recipe.synthesis_recipe_snapshot",
        lambda resolved, _args, *, target: {"target": target, "resolved": resolved.name},
    )
    synth = acceptance_targets.CriterionTarget(
        "mandatory", "synthesis_ok", "candidate", "synth", True, "baseline"
    )
    monkeypatch.setattr(acceptance_targets, "validate_criterion_targets", lambda *_args: [])
    monkeypatch.setattr(acceptance_targets, "criterion_targets", lambda *_args: (synth,))
    monkeypatch.setattr(acceptance_targets, "_missing_target_sources", lambda *_args: [])
    monkeypatch.setattr(acceptance_targets, "_dry_resolve_binding", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "booley.flows.recipe_evidence.implementation_comparison_basis", lambda value: value
    )
    monkeypatch.setattr("booley.flows.recipe_evidence.recipe_changes", lambda *_args: [])
    assert acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build") == []
    other = acceptance_targets.CriterionTarget(
        "mandatory", "sim_pass", "candidate", "sim", False, "baseline"
    )
    monkeypatch.setattr(acceptance_targets, "criterion_targets", lambda *_args: (other,))
    assert acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build") == []


def test_comparison_snapshots_dispatch_fpga(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda target, **_kwargs: SimpleNamespace(name=target),
    )
    monkeypatch.setattr(
        "booley.flows.fpga.recipe.fpga_recipe_snapshot",
        lambda resolved, *, target: {"target": target, "resolved": resolved.name},
    )
    binding = acceptance_targets.CriterionTarget(
        "mandatory", "fpga_impl_ok", "candidate", "fpga", True, "baseline"
    )
    monkeypatch.setattr(acceptance_targets, "validate_criterion_targets", lambda *_args: [])
    monkeypatch.setattr(acceptance_targets, "criterion_targets", lambda *_args: (binding,))
    monkeypatch.setattr(acceptance_targets, "_missing_target_sources", lambda *_args: [])
    monkeypatch.setattr(acceptance_targets, "_dry_resolve_binding", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "booley.flows.recipe_evidence.implementation_comparison_basis", lambda value: value
    )
    monkeypatch.setattr("booley.flows.recipe_evidence.recipe_changes", lambda *_args: [])
    assert acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build") == []


def test_dry_resolve_binding_reports_failure_and_missing_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.CriterionTarget("mandatory", "lint_clean", "lint", "lint", False)
    monkeypatch.setattr(acceptance_targets, "validate_criterion_targets", lambda *_args: [])
    monkeypatch.setattr(acceptance_targets, "criterion_targets", lambda *_args: (binding,))
    monkeypatch.setattr(acceptance_targets, "_missing_target_sources", lambda *_args: [])
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    assert (
        "dry-run failed"
        in acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build")[0]
    )
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda *_args, **_kwargs: SimpleNamespace(toplevel=""),
    )
    assert (
        "without a toplevel"
        in acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build")[0]
    )


def test_validate_binding_reports_resolution_flow_and_scope_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.CriterionTarget(
        "mandatory", "synthesis_ok", "candidate", "synth", True, "baseline"
    )

    def select(_root: Path, target: str) -> SimpleNamespace:
        if target == "candidate":
            raise acceptance_targets.fusesoc_registry.FuseSocError("unknown")
        return SimpleNamespace(flow="sim", eda_tool="iverilog")

    monkeypatch.setattr(acceptance_targets, "select_target", select)
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: False)
    monkeypatch.setattr(acceptance_targets, "criterion_targets", lambda *_args: (binding,))
    errors = acceptance_targets.validate_criterion_targets({}, tmp_path)
    assert "candidate target 'candidate': unknown" in errors[0]
    assert "cannot satisfy synthesis_ok" in errors[1]

    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda *_args: SimpleNamespace(flow="synth", eda_tool="yosys"),
    )
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: True)
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda _root, target: (_target_input(f"rtl/{target}.sv"),),
    )
    errors = acceptance_targets.validate_criterion_targets({"scope": []}, tmp_path)
    assert "not declared Scope [new]" in errors[0]
    assert "relative-QoR baseline" in errors[1]


def test_validate_binding_selectors_reports_failure_and_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.AcceptanceTargetBinding(
        "sim",
        "criteria.mandatory.sim_pass",
        "expected-base",
        "expected-candidate",
        "base",
        "candidate",
    )

    def select(_root: Path, selector: str) -> SimpleNamespace:
        if selector == "base":
            raise ValueError("missing")
        return SimpleNamespace(identity="actual")

    monkeypatch.setattr(acceptance_targets, "select_target", select)
    errors = acceptance_targets.validate_binding_selectors(tmp_path, [binding])
    assert "cannot be resolved" in errors[0]
    assert "resolves to 'actual'" in errors[1]


def test_resolve_commit_rejects_nonexact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", stdout="b" * 40 + "\n"),
    )
    with pytest.raises(ValueError, match="does not resolve exactly"):
        acceptance_targets.resolve_commit(tmp_path, "a" * 40)
