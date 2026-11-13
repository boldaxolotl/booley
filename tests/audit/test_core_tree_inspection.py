"""The ``.core`` tree audit owner returns complete reports; its rules need no Doctor.

Every assertion reads the returned ``DiagnosticReport``: severity, message, fix,
check ID, subject and the uncounted details. The owner module is imported inside
a helper so this file collects, and each test fails on its own, before the owner
exists.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

from tests.conftest import symlink_or_skip

from booley.audit import target_matrix
from booley.audit.diagnostic_results import DiagnosticDetail, DiagnosticReport, Severity
from booley.fusesoc import fusesoc_registry

_BAD_DEPEND_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  tb:
    files: [tb/tb.sv: {file_type: systemVerilogSource}]
    depend: not_a_list
targets:
  sim:
    flow: sim
    flow_options: {booley: {doctor: [sim]}}
    filesets: [tb]
"""

_BAD_FILE_KEY_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files: [rtl/dut.sv: {file_type: systemVerilogSource, vendored: true}]
targets:
  sim:
    flow: sim
    flow_options: {booley: {doctor: [sim]}}
    filesets: [rtl]
"""

_GOOD_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  lint: {flow: lint, flow_options: {tool: verilator}, filesets: [rtl], toplevel: dut}
"""

_UNKNOWN_PROPERTY_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  lint:
    filesetz: [rtl]
    flow: lint
    flow_options: {tool: verilator, booley: {doctor: [lint]}}
    filesets: [rtl]
    toplevel: dut
"""

_SOLE_MISPLACED_CORE = """\
CAPI=2:
name: ::solo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  fpga_core:
    flow: generic
    flow_options: {tool: vivado, part: xc7a35tcsg324-1}
    booley: {doctor: [fpga]}
    filesets: [rtl]
    toplevel: dut
"""

_PROVIDER_CORE = """\
CAPI=2:
name: ::demo:0
provider: {name: github, user: acme, repo: demo}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
"""

_SIM_CORE_WITH_GENERATOR = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb: {files: [tb/tb_dut.sv], tags: [tb]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
    filesets: [rtl, tb]
    toplevel: tb_dut
generators:
  check_reqs:
    command: rtl/check_tool_requirements.py
"""

_EXPRESSION_PARAMETER_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb: {files: [tb/tb_dut.sv], tags: [tb]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: tb_dut
parameters:
  width:
    datatype: expression
    paramtype: vlogparam
"""

_FPGA_HOOK_CORE = """\
CAPI=2:
name: ::demo:0
targets:
  impl:
    flow_options: {tool: vivado}
    hooks:
      post_build: [run_bitstream]
"""

_NO_HAZARDS = ".core tree has no provider or recursive-symlink setup hazards"


def _owner() -> ModuleType:
    """The ``.core`` tree owner module, imported where used."""
    return importlib.import_module("booley.audit.core_tree_inspection")


def _write_core(root: Path, text: str, name: str = "design.core") -> Path:
    core = root / name
    core.write_text(text, encoding="utf-8", newline="\n")
    return core


def _tree_report(root: Path) -> DiagnosticReport:
    """Setup hazards then schema, with the seed Targets Doctor would hand over."""
    owner = _owner()
    seeds = target_matrix.build_doctor_target_matrix(root).seed_targets
    return owner.inspect_core_tree(owner.CoreTreeInspectionRequest(root, seeds))


def _security_report(root: Path, write_scope: tuple[str, ...] = ()) -> DiagnosticReport:
    owner = _owner()
    return owner.inspect_core_security(owner.CoreSecurityInspectionRequest(root, write_scope))


def _rows(report: DiagnosticReport, severity: Severity) -> list[str]:
    return [finding.message for finding in report.findings if finding.severity is severity]


def _ancestor_links(root: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        library = root / f"lib{index:02d}"
        library.mkdir()
        symlink_or_skip(library / "self", "..", target_is_directory=True)
        paths.append(f"lib{index:02d}/self")
    return paths


def _recursive_hazard(
    root: Path, relative: str, target: Path | None
) -> fusesoc_registry.CoreSetupHazard:
    detail = (
        "symlink chain contains a cycle" if target is None else f"resolves to ancestor {target}"
    )
    return fusesoc_registry.CoreSetupHazard("recursive-symlink", root / relative, detail, target)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_violation_in_driven_core_is_one_fail(tmp_path):
    """A violation in a core the project drives is exactly one FAIL.

    The Booley subset and FuseSoC's own verdict describe the same CAPI2 entry,
    so the FuseSoC row must not duplicate the subset row.
    """
    for name, core_text, expected in (
        ("scalar-depend", _BAD_DEPEND_CORE, "depend must be array"),
        ("per-file-key", _BAD_FILE_KEY_CORE, "vendored"),
    ):
        root = tmp_path / name
        root.mkdir()
        _write_core(root, core_text)
        failures = [f for f in _tree_report(root).findings if f.severity is Severity.FAIL]
        assert len(failures) == 1, name
        assert expected in failures[0].message
        assert failures[0].message.startswith(".core schema design.core:")
        assert "fix the .core" in failures[0].fix


def test_vendored_core_schema_violation_is_a_note(tmp_path):
    """A core no Doctor Target selects is a NOTE: FuseSoC skips it at resolve, the
    configured flows are unaffected, and 'fix the .core' is unfollowable for a
    pristine upstream checkout (the YosysHQ picorv32.core ships a bare ``depend:``)."""
    _write_core(
        tmp_path,
        _BAD_DEPEND_CORE.replace("    flow_options: {booley: {doctor: [sim]}}\n", ""),
    )
    report = _tree_report(tmp_path)
    assert not report.active_findings
    assert any(
        "depend must be array" in message and "vendored" in message
        for message in _rows(report, Severity.NOTE)
    )


def test_state_zone_core_schema_violation_fails_unconfigured(tmp_path):
    """State-zone cores (ADR 0036) are always Booley-authored, so a violation is ours."""
    state_cores = tmp_path / ".booley" / "cores"
    state_cores.mkdir(parents=True)
    _write_core(state_cores, _BAD_DEPEND_CORE)
    assert any("depend must be array" in m for m in _rows(_tree_report(tmp_path), Severity.FAIL))


def test_clean_core_passes_the_schema_row(tmp_path):
    _write_core(tmp_path, _GOOD_CORE)
    report = _tree_report(tmp_path)
    assert not report.active_findings
    assert ".core CAPI2 schema valid" in _rows(report, Severity.PASS)


def test_unknown_target_property_is_one_fail_naming_the_key(tmp_path):
    _write_core(tmp_path, _UNKNOWN_PROPERTY_CORE)
    (failure,) = [f for f in _tree_report(tmp_path).findings if f.severity is Severity.FAIL]
    assert "design.core" in failure.message
    assert "targets.lint" in failure.message
    assert "filesetz" in failure.message
    assert "flow_options" not in failure.fix


def test_misplaced_booley_in_sole_target_core_is_an_owned_fail_with_hint(tmp_path):
    """The misplaced marker is the core's only Doctor intent, yet the core is driven.

    No Doctor-selected Target remains for Booley's reader, so ownership comes from
    the misplaced marker; otherwise the row is a hint-less vendored-core NOTE.
    """
    _write_core(tmp_path, _SOLE_MISPLACED_CORE)
    report = _tree_report(tmp_path)
    assert _rows(report, Severity.NOTE) == []
    (failure,) = [f for f in report.findings if f.severity is Severity.FAIL]
    assert "fpga_core" in failure.message
    assert "targets.fpga_core.flow_options" in failure.fix


def test_booley_only_schema_row_does_not_hide_fusesoc_reason(tmp_path):
    """An unsupported Booley key does not say why FuseSoC ignores the file."""
    _write_core(
        tmp_path,
        _UNKNOWN_PROPERTY_CORE.replace("{doctor: [lint]}", "{doctor: [lint], bogus: 1}"),
    )
    failures = _rows(_tree_report(tmp_path), Severity.FAIL)
    assert any("not a supported Booley Target key" in message for message in failures)
    assert any("filesetz" in message for message in failures)


# ---------------------------------------------------------------------------
# Setup hazards
# ---------------------------------------------------------------------------


def test_selected_provider_block_fails(tmp_path):
    core = _write_core(tmp_path, _PROVIDER_CORE)
    (failure,) = [
        f for f in _tree_report(tmp_path).findings if f.message.startswith("in-tree .core")
    ]
    assert failure.severity is Severity.FAIL
    assert core.name in failure.message
    assert "remote fetch" in failure.message
    assert failure.fix == "remove provider: from the in-tree core"


def test_unselected_provider_block_is_a_note(tmp_path):
    _write_core(
        tmp_path,
        _PROVIDER_CORE.replace("flow_options: {tool: verilator, booley: {doctor: [sim]}}", ""),
    )
    report = _tree_report(tmp_path)
    assert not [m for m in _rows(report, Severity.FAIL) if "remote fetch" in m]
    assert any("no Doctor Target selects it" in m for m in _rows(report, Severity.NOTE))


def test_provider_block_in_dependency_closure_fails(tmp_path):
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: acme:demo:top:0
filesets:
  rtl:
    depend: [acme:demo:dep]
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
    filesets: [rtl]
""",
        name="top.core",
    )
    dep = _write_core(
        tmp_path,
        """\
CAPI=2:
name: acme:demo:dep:0
provider: {name: github, user: acme, repo: dep}
targets:
  default: {}
""",
        name="dep.core",
    )
    (failure,) = [
        f for f in _tree_report(tmp_path).findings if f.message.startswith("in-tree .core")
    ]
    assert dep.name in failure.message


def test_recursive_links_group_with_verbose_details(tmp_path, monkeypatch):
    """One FAIL per resolved ancestor, previewing at most three links; every link is
    a verbose-only detail following that FAIL."""
    for count in (1, 3, 5):
        root = tmp_path / f"links{count}"
        paths = [f"lib{index}/self" for index in range(count)]
        hazards = [_recursive_hazard(root, path, root) for path in reversed(paths)]
        monkeypatch.setattr(fusesoc_registry, "core_setup_hazards", lambda _root, h=hazards: h)
        root.mkdir()

        report = _tree_report(root)

        group = next(
            i for i, f in enumerate(report.findings, 1) if "recursive symlinks" in f.message
        )
        message = report.findings[group - 1].message
        noun = "link" if count == 1 else "links"
        assert f"{count} {noun}" in message
        assert ", ".join(paths[:3]) in message
        assert ("more" in message) is (count > 3)
        assert report.findings[group - 1].severity is Severity.FAIL
        assert report.findings[group - 1].fix.startswith("add a FUSESOC_IGNORE marker")
        assert report.details == tuple(
            DiagnosticDetail(
                group, f"  recursive symlink {path}: directory link resolves to ancestor {root}"
            )
            for path in paths
        )
        assert all(detail.verbose_only for detail in report.details)


def test_different_ancestors_and_unresolved_cycles_are_separate(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    hazards = [
        _recursive_hazard(tmp_path, "a/self", tmp_path),
        _recursive_hazard(tmp_path, "nested/a/self", nested),
        _recursive_hazard(tmp_path, "b/self", tmp_path),
        # Legacy scanner cycle hazards carry no resolved ancestor.
        _recursive_hazard(tmp_path, "cycle-a", None),
        _recursive_hazard(tmp_path, "cycle-b", None),
    ]
    monkeypatch.setattr(fusesoc_registry, "core_setup_hazards", lambda _root: hazards)

    failures = _rows(_tree_report(tmp_path), Severity.FAIL)

    assert len(failures) == 4
    assert sum("2 links" in message and str(tmp_path) in message for message in failures) == 1
    assert sum("1 link" in message and str(nested) in message for message in failures) == 1
    assert sum("symlink chain contains a cycle" in message for message in failures) == 2
    assert any("cycle-a" in message for message in failures)
    assert any("cycle-b" in message for message in failures)


def test_recursive_group_does_not_hide_selected_provider(tmp_path):
    _write_core(tmp_path, _PROVIDER_CORE)
    _ancestor_links(tmp_path, 5)
    failures = _rows(_tree_report(tmp_path), Severity.FAIL)
    assert sum("remote fetch" in message for message in failures) == 1
    assert sum("5 links" in message for message in failures) == 1


def test_no_recursive_links_keeps_the_hazard_pass(tmp_path):
    report = _tree_report(tmp_path)
    assert _rows(report, Severity.PASS) == [_NO_HAZARDS, ".core CAPI2 schema valid"]
    assert not report.active_findings
    assert report.details == ()


def test_ignored_link_subtree_keeps_the_hazard_pass(tmp_path):
    _ancestor_links(tmp_path, 1)
    (tmp_path / "lib00" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    assert _NO_HAZARDS in _rows(_tree_report(tmp_path), Severity.PASS)


def test_real_links_to_different_ancestors_group_by_resolved_target(tmp_path):
    for directory, target in (("a", ".."), ("nested/a", "../.."), ("nested/b", "..")):
        library = tmp_path / directory
        library.mkdir(parents=True)
        link = library / "self"
        # Windows relative symlink targets require native separators.
        symlink_or_skip(link, Path(target), target_is_directory=True)
        assert link.resolve(strict=True) == (library / target).resolve(strict=True)
    failures = _rows(_tree_report(tmp_path), Severity.FAIL)
    assert len(failures) == 2
    assert any(f"2 links resolve to ancestor {tmp_path.resolve()} (" in m for m in failures)
    assert any(
        f"1 link resolves to ancestor {(tmp_path / 'nested').resolve()} (" in m for m in failures
    )


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_in_scope_script_is_a_scope_advisory_warning(tmp_path):
    """Doctor's Scope is synthetic, so a Scope-dependent violation cannot hard-fail.

    Ibex's upstream ``check_tool_requirements.core`` references a script under a
    writable directory the configured Targets never invoke; the binding check is
    the change-specific Scope at commit time.
    """
    _write_core(tmp_path, _SIM_CORE_WITH_GENERATOR)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "check_tool_requirements.py").write_text("#!/usr/bin/env python3\n")

    report = _security_report(tmp_path, ("rtl/*",))

    assert not [f for f in report.findings if f.severity is Severity.FAIL]
    (warning,) = [f for f in report.findings if f.severity is Severity.WARN]
    assert warning.check_id == "core.security-scope-advisory"
    assert warning.subject == "design.core:-"
    assert "[in_scope_script]" in warning.message
    # The advisory must say why it is not a gate, or it reads as a bug.
    assert "at commit time" in warning.message


def test_out_of_scope_script_passes(tmp_path):
    _write_core(tmp_path, _SIM_CORE_WITH_GENERATOR)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "check_tool_requirements.py").write_text("#!/usr/bin/env python3\n")
    report = _security_report(tmp_path, ("tb/*",))
    assert not report.active_findings
    assert _rows(report, Severity.PASS) == [
        ".core security validation passed (no fpga hooks / expr-params / in-scope scripts)"
    ]


def test_expression_parameter_fails_security(tmp_path):
    """Scope-independent violations are properties of the .core itself."""
    _write_core(tmp_path, _EXPRESSION_PARAMETER_CORE)
    (failure,) = _security_report(tmp_path).active_findings
    assert failure.severity is Severity.FAIL
    assert failure.message.startswith(".core security [expr_param] design.core:")
    assert failure.fix == "ADR 0022 decision 21"
    assert failure.check_id is None


def test_fpga_hook_fails_security(tmp_path):
    _write_core(tmp_path, _FPGA_HOOK_CORE)
    failures = _rows(_security_report(tmp_path), Severity.FAIL)
    assert any("[fpga_hook]" in message for message in failures)


def test_security_pass_needs_at_least_one_target(tmp_path):
    assert _security_report(tmp_path).findings == ()


# ---------------------------------------------------------------------------
# Ownership helpers
# ---------------------------------------------------------------------------


def test_owned_core_files_are_selected_and_state_zone_cores(tmp_path):
    selected = _write_core(tmp_path, _PROVIDER_CORE, name="selected.core")
    _write_core(tmp_path, _GOOD_CORE.replace("::demo:0", "::vendored:0"), name="vendored.core")
    state_cores = tmp_path / ".booley" / "cores"
    state_cores.mkdir(parents=True)
    state = _write_core(state_cores, _GOOD_CORE.replace("::demo:0", "::state:0"), name="s.core")

    seeds = target_matrix.build_doctor_target_matrix(tmp_path).seed_targets

    assert _owner().owned_core_files(tmp_path, seeds) == {selected, state}
    assert _owner().owned_core_files(tmp_path, ("no_such_target",)) == {state}


def test_rejected_target_core_names_the_core_fusesoc_ignores(tmp_path):
    core = _write_core(tmp_path, _BAD_DEPEND_CORE)
    rejected = _owner().fusesoc_rejected_cores(tmp_path)

    assert list(rejected) == [core]
    assert _owner().rejected_target_core(tmp_path, "sim", rejected) == core
    assert _owner().rejected_target_core(tmp_path, "no_such_target", rejected) is None
    assert _owner().rejected_target_core(tmp_path, "sim", {}) is None


def test_no_rejected_cores_in_a_valid_tree(tmp_path):
    _write_core(tmp_path, _GOOD_CORE)
    assert _owner().fusesoc_rejected_cores(tmp_path) == {}
