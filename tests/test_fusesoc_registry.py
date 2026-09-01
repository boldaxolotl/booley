"""Tests for fusesoc_registry: .core enumeration + EDAM resolution (ADR 0022)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booley.fusesoc import selftest_overlay
from booley.fusesoc.fusesoc_registry import (
    DEFAULT_FUSESOC_CMD,
    STATE_CORES_SUBDIR,
    TRACE_OVERLAY_MARKER,
    AmbiguousTargetError,
    CoreCollisionError,
    FuseSocError,
    IncompatibleTargetError,
    MissingSourceError,
    ResolvedTarget,
    TargetResolutionError,
    TraceMode,
    UnknownTargetError,
    _enumerate_all,
    all_referenced_files,
    available_targets,
    core_schema_errors,
    core_setup_hazards,
    core_target_doctor_flows,
    core_target_eda_tool,
    core_target_flow,
    core_target_flow_option,
    core_target_is_doctor_selftest,
    core_target_names,
    core_target_uses_legacy_fusesoc_api,
    discover_cores,
    doctor_target_seed,
    doctor_target_selectors,
    enumerate_targets,
    missing_target_sources,
    parse_edam,
    preflight_target_sources,
    read_core,
    resolve_ref,
    resolve_target,
    resolve_target_selection,
    selectable_core_closure,
    sim_target_has_untagged_tb,
    state_cores_dir,
    target_eda_tools,
    target_referenced_files,
    target_source_files,
    target_source_files_for_ref,
    trace_overlay_vlnv,
    try_resolve_target,
    vendored_files,
    write_trace_overlay,
)
from tests.conftest import require_symlinks, symlink_or_skip

# ---------------------------------------------------------------------------
# Fixtures: a .core and a resolved EDAM matching the Phase-2 spike shape.
# ---------------------------------------------------------------------------

_CORE_TEXT = textwrap.dedent(
    """\
    CAPI=2:
    name: ::demo_core:0
    description: spike core

    filesets:
      rtl:
        files:
          - rtl/counter_pkg.sv: {file_type: systemVerilogSource}
          - rtl/counter.sv: {file_type: systemVerilogSource}
      tb:
        files:
          - tb/tb_counter.sv: {file_type: systemVerilogSource}
        tags: [tb]

    targets:
      default:
        filesets: [rtl]
      sim:
        default_tool: verilator
        flow: sim
        flow_options:
          tool: verilator
        filesets: [rtl, tb]
        toplevel: tb_counter
    """
)


def test_malformed_unselected_core_does_not_hide_valid_targets(tmp_path: Path) -> None:
    (tmp_path / "valid.core").write_text(_CORE_TEXT, encoding="utf-8")
    (tmp_path / "legacy.core").write_text("targets: [unterminated", encoding="utf-8")

    assert "sim" in available_targets(tmp_path)


# Exactly the shape `fusesoc run --setup` emits (captured from the spike).
_EDAM_TEXT = textwrap.dedent(
    """\
    version: 0.2.1
    name: demo_core_0
    toplevel: tb_counter
    parameters:
      TESTID:
        datatype: int
        paramtype: plusarg
        description: test selector
    flow_options:
      tool: verilator
      flatten: false
    files:
    - file_type: systemVerilogSource
      name: src/demo_core_0/rtl/counter_pkg.sv
      core: '::demo_core:0'
    - file_type: systemVerilogSource
      name: src/demo_core_0/rtl/counter.sv
      core: '::demo_core:0'
    - file_type: systemVerilogSource
      tags:
      - tb
      name: src/demo_core_0/tb/tb_counter.sv
      core: '::demo_core:0'
    """
)


def _touch_declared_sources(core: Path) -> None:
    """Create empty files for every literal fileset path *core* declares.

    ``resolve_target`` now preflights fileset-path existence before invoking
    fusesoc (MissingSourceError), so fixtures create the declared sources by
    default; the preflight's own tests write their cores without this helper.
    """
    try:
        doc = read_core(core)
    except FuseSocError:
        return  # deliberately malformed fixture — nothing to create
    filesets = doc.get("filesets")
    if not isinstance(filesets, dict):
        return
    for fs in filesets.values():
        files = fs.get("files") if isinstance(fs, dict) else None
        if not isinstance(files, list):
            continue
        for entry in files:
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, dict) and len(entry) == 1:
                name = str(next(iter(entry)))
            else:
                continue
            if name.endswith("booley_vcd_dump.sv"):
                # The trace-overlay tests own this file's lifecycle (its
                # presence/content drives write_trace_overlay's $dumpfile
                # validation), so the fixture never pre-creates it.
                continue
            path = core.parent / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()


def _write_core(directory: Path, text: str = _CORE_TEXT, *, create_sources: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    core = directory / "design.core"
    core.write_text(text, encoding="utf-8")
    if create_sources:
        _touch_declared_sources(core)
    return core


# ---------------------------------------------------------------------------
# discover_cores / read_core / core_target_names
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discovers_authored_cores(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        _write_core(tmp_path / "ip" / "sub")
        found = discover_cores(tmp_path)
        assert len(found) == 2
        assert all(p.suffix == ".core" for p in found)

    def test_skips_build_and_runtime_trees(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        _write_core(tmp_path / "build" / "demo_core_0")  # generated artifact
        _write_core(tmp_path / ".runtime" / "x")  # generated artifact
        found = discover_cores(tmp_path)
        assert [p.parent.name for p in found] == ["ip"]

    def test_skips_fusesoc_ignore_trees(self, tmp_path: Path):
        # Mirrors FuseSoC's scanner convention: a FUSESOC_IGNORE marker file
        # excludes that directory and everything below it.
        _write_core(tmp_path / "ip")
        _write_core(tmp_path / "vendored")
        _write_core(tmp_path / "vendored" / "nested")
        (tmp_path / "vendored" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        found = discover_cores(tmp_path)
        assert [p.parent.name for p in found] == ["ip"]

    def test_skips_booley_project_state_tree(self, tmp_path: Path):
        # Per-ticket / baseline git worktrees under .booley_project/ carry stale
        # VLNV-colliding copies of the project's cores; they must never be
        # enumerated (they'd shadow the repo-root source and drop its Targets).
        _write_core(tmp_path / "ip")
        _write_core(tmp_path / ".booley_project" / "worktrees" / "scalar_1bfe1733")
        _write_core(tmp_path / ".booley_project" / ".baseline-wt-123-abc")
        found = discover_cores(tmp_path)
        assert [p.parent.name for p in found] == ["ip"]

    def test_read_core_parses_capi2_header(self, tmp_path: Path):
        doc = read_core(_write_core(tmp_path))
        assert doc["name"] == "::demo_core:0"
        assert set(doc["targets"]) == {"default", "sim"}

    def test_read_core_rejects_non_mapping(self, tmp_path: Path):
        bad = tmp_path / "bad.core"
        bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(FuseSocError):
            read_core(bad)

    def test_target_names_excludes_default(self, tmp_path: Path):
        doc = read_core(_write_core(tmp_path))
        assert core_target_names(doc) == ["sim"]


class TestCoreSetupHazards:
    def test_reports_provider_block(self, tmp_path: Path):
        text = _CORE_TEXT.replace(
            "description: spike core",
            "description: spike core\nprovider: {name: github, user: acme, repo: demo}",
        )
        core = _write_core(tmp_path, text)
        hazards = core_setup_hazards(tmp_path)
        assert [(h.kind, h.path) for h in hazards] == [("provider", core)]

    def test_reports_directory_link_back_to_ancestor(self, tmp_path: Path):
        library = tmp_path / "lib"
        library.mkdir()
        link = library / "repo"
        symlink_or_skip(link, tmp_path, target_is_directory=True)
        hazards = core_setup_hazards(tmp_path)
        assert [(h.kind, h.path) for h in hazards] == [("recursive-symlink", link)]
        assert "ancestor" in hazards[0].detail

    def test_fusesoc_ignore_suppresses_recursive_link(self, tmp_path: Path):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        symlink_or_skip(library / "repo", tmp_path, target_is_directory=True)
        assert core_setup_hazards(tmp_path) == []

    def test_fusesoc_ignore_at_project_scan_root_suppresses_recursive_link(self, tmp_path: Path):
        (tmp_path / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        library = tmp_path / "lib"
        library.mkdir()
        symlink_or_skip(library / "repo", tmp_path, target_is_directory=True)
        assert core_setup_hazards(tmp_path) == []

    def test_fusesoc_ignore_at_state_cores_root_suppresses_recursive_link(self, tmp_path: Path):
        stealth_root = state_cores_dir(tmp_path)
        library = stealth_root / "lib"
        library.mkdir(parents=True)
        (stealth_root / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        symlink_or_skip(library / "cores", stealth_root, target_is_directory=True)
        assert core_setup_hazards(tmp_path) == []

    def test_non_recursive_directory_link_is_not_reported(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        links = tmp_path / "links"
        links.mkdir()
        symlink_or_skip(links / "source", source, target_is_directory=True)
        assert core_setup_hazards(tmp_path) == []


# ---------------------------------------------------------------------------
# Stealth authored cores: .booley_project/cores/ as a second root (ADR 0036)
# ---------------------------------------------------------------------------


class TestStealthCores:
    """Stealth projects (Booley usage invisible to the host repo) author their
    cores under .booley_project/cores/ — the one state-dir subtree scanned as
    a second discovery root and handed to FuseSoC as a second --cores-root."""

    def test_state_cores_dir_is_structural(self, tmp_path: Path):
        assert state_cores_dir(tmp_path) == tmp_path / ".booley_project" / STATE_CORES_SUBDIR

    def test_stealth_cores_discovered_worktrees_still_skipped(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(state_cores_dir(tmp_path), stealth)
        # Transient state stays invisible even with the stealth root active.
        _write_core(tmp_path / ".booley_project" / "worktrees" / "t1")
        _write_core(tmp_path / ".booley_project" / ".baseline-wt-9-x")
        found = discover_cores(tmp_path)
        assert sorted(p.parent.name for p in found) == [STATE_CORES_SUBDIR, "ip"]

    def test_state_dir_marker_does_not_veto_stealth_root(self, tmp_path: Path):
        # Deployed reality: booley init drops FUSESOC_IGNORE at .booley_project/.
        # The stealth root is scanned as a ROOT, so the ancestor marker (which
        # gates the repo-root walk) must not hide the authored cores.
        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(state_cores_dir(tmp_path), stealth)
        (tmp_path / ".booley_project" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        assert [p.parent.name for p in discover_cores(tmp_path)] == [STATE_CORES_SUBDIR]

    def test_marker_inside_stealth_root_hides_it(self, tmp_path: Path):
        # A FUSESOC_IGNORE *at or under* the stealth root is honored — same
        # semantics FuseSoC's own walk applies to a scan root.
        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(state_cores_dir(tmp_path), stealth)
        (state_cores_dir(tmp_path) / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        assert discover_cores(tmp_path) == []

    def test_cross_root_vlnv_collision_is_fatal(self, tmp_path: Path):
        # Same logical VLNV in both roots = the worktree-shadowing bug reborn
        # as an authoring mistake; enumeration refuses rather than pick a side.
        _write_core(tmp_path / "ip")
        _write_core(state_cores_dir(tmp_path))  # identical ::demo_core:0
        with pytest.raises(CoreCollisionError, match="both core roots"):
            enumerate_targets(tmp_path)

    def test_cross_root_collision_ignores_version_segment(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        other_version = _CORE_TEXT.replace("::demo_core:0", "::demo_core:1")
        _write_core(state_cores_dir(tmp_path), other_version)
        with pytest.raises(CoreCollisionError, match="both core roots"):
            enumerate_targets(tmp_path)

    def test_same_zone_version_dedup_stays_legal(self, tmp_path: Path):
        # Two *versions* of one core in the SAME zone keep first-wins semantics.
        stealth_v0 = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        stealth_v1 = _CORE_TEXT.replace("::demo_core:0", "::stealth:1")
        _write_core(state_cores_dir(tmp_path) / "a", stealth_v0)
        _write_core(state_cores_dir(tmp_path) / "b", stealth_v1)
        refs = enumerate_targets(tmp_path)
        assert set(refs) == {"sim"}

    def test_setup_command_adds_second_cores_root(self, tmp_path: Path):
        from booley.fusesoc.fusesoc_registry import setup_command

        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(state_cores_dir(tmp_path), stealth)
        cmd = setup_command("sim", project_root=tmp_path, build_root=tmp_path / "b")
        roots = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cores-root"]
        assert roots == [str(tmp_path), str(state_cores_dir(tmp_path))]
        # Global option: both must precede the 'run' subcommand.
        assert max(i for i, a in enumerate(cmd) if a == "--cores-root") < cmd.index("run")

    def test_setup_command_omits_stealth_root_when_absent(self, tmp_path: Path):
        from booley.fusesoc.fusesoc_registry import setup_command

        _write_core(tmp_path / "ip")
        cmd = setup_command("sim", project_root=tmp_path, build_root=tmp_path / "b")
        assert cmd.count("--cores-root") == 1

    def test_explicit_stealth_projects_core_into_repo_root(self, tmp_path: Path):
        from booley.fusesoc.fusesoc_registry import setup_command

        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        core_text = (
            _CORE_TEXT.replace("rtl/counter_pkg.sv", "counter_pkg.sv")
            .replace("rtl/counter.sv", "counter.sv")
            .replace("tb/tb_counter.sv", "tb_counter.sv")
        )
        canonical = _write_core(state_cores_dir(tmp_path), core_text, create_sources=False)
        for name in ("counter_pkg.sv", "counter.sv", "tb_counter.sv"):
            (tmp_path / name).touch()

        cmd = setup_command("sim", project_root=tmp_path, build_root=tmp_path / "b")
        projected = tmp_path / ".booley-projected-design.core"

        assert cmd.count("--cores-root") == 1
        assert projected.is_file()
        assert discover_cores(tmp_path) == [canonical]
        sources = target_source_files(tmp_path, "sim")
        assert sources.rtl_source_files == ("counter_pkg.sv", "counter.sv")
        assert sources.tb_files == ("tb_counter.sv",)
        assert missing_target_sources(tmp_path, "sim") == []

    def test_native_core_ignore_uses_only_private_stealth_registry(self, tmp_path: Path):
        from booley.fusesoc.core_projection import isolated_registry_root
        from booley.fusesoc.fusesoc_registry import setup_command

        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        canonical = _write_core(state_cores_dir(tmp_path), create_sources=False)
        (tmp_path / "native.core").write_text("CAPI=1\n", encoding="utf-8")

        cmd = setup_command("sim", project_root=tmp_path, build_root=tmp_path / "b")

        roots = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--cores-root"]
        assert roots == [str(isolated_registry_root(tmp_path))]
        assert discover_cores(tmp_path) == [canonical]

    def test_setup_command_refuses_foreign_expected_projection(self, tmp_path: Path):
        from booley.fusesoc.core_projection import projected_core_path
        from booley.fusesoc.fusesoc_registry import setup_command

        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\n",
            encoding="utf-8",
        )
        canonical = _write_core(state_cores_dir(tmp_path), create_sources=False)
        projected = projected_core_path(tmp_path, canonical)
        foreign_content = "CAPI=2:\nname: foreign::core:0\n"
        projected.write_text(foreign_content, encoding="utf-8")

        with pytest.raises(TargetResolutionError, match="refusing to overwrite non-Booley file"):
            setup_command("sim", project_root=tmp_path, build_root=tmp_path / "b")

        assert projected.read_text(encoding="utf-8") == foreign_content


# ---------------------------------------------------------------------------
# enumerate_targets
# ---------------------------------------------------------------------------


class TestEnumerate:
    def test_maps_name_to_core_and_vlnv(self, tmp_path: Path):
        core = _write_core(tmp_path / "ip")
        refs = enumerate_targets(tmp_path)
        assert set(refs) == {"sim"}
        assert refs["sim"].vlnv == "::demo_core:0"
        assert refs["sim"].core_file == core
        assert "default" not in refs  # implicit target is never selectable

    def test_duplicate_name_across_distinct_cores_is_legal(self, tmp_path: Path):
        # ADR 0030: two distinct cores declaring the same bare Target name is
        # legal FuseSoC (ibex: 'lint' on 54 cores). enumerate_targets never
        # raises — it exposes a first-wins view; _enumerate_all keeps both.
        _write_core(tmp_path / "a")  # ::demo_core:0 declares 'sim'
        second = _CORE_TEXT.replace("::demo_core:0", "::other:0")
        _write_core(tmp_path / "b", second)  # ::other:0 also declares 'sim'

        # First-wins view (discover_cores order: a/ before b/).
        refs = enumerate_targets(tmp_path)
        assert set(refs) == {"sim"}
        assert refs["sim"].vlnv == "::demo_core:0"

        # _enumerate_all keeps every declaring core, both distinct VLNVs.
        allrefs = _enumerate_all(tmp_path)
        assert {r.vlnv for r in allrefs["sim"]} == {"::demo_core:0", "::other:0"}

        # A bare name is now ambiguous; each vlnv#name qualifier resolves.
        with pytest.raises(AmbiguousTargetError, match="declared by 2 cores"):
            resolve_ref(tmp_path, "sim")
        assert resolve_ref(tmp_path, "demo_core#sim").vlnv == "::demo_core:0"
        assert resolve_ref(tmp_path, "other#sim").vlnv == "::other:0"

    def test_core_without_name_is_skipped(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        nameless = _CORE_TEXT.replace("name: ::demo_core:0\n", "")
        _write_core(tmp_path / "nameless", nameless)
        refs = enumerate_targets(tmp_path)
        assert set(refs) == {"sim"}

    def test_target_eda_tool_read_from_flow_options(self, tmp_path: Path):
        """TargetRef.eda_tool carries the declared flow_options.eda_tool (decision 11)."""
        _write_core(tmp_path / "ip")
        refs = enumerate_targets(tmp_path)
        assert refs["sim"].eda_tool == "verilator"


# ---------------------------------------------------------------------------
# core_target_eda_tool — declared EDA tool, read from .core YAML
# ---------------------------------------------------------------------------


class TestCoreTargetEdaTool:
    def test_prefers_upstream_flow_options_tool(self):
        doc = {"targets": {"sim": {"flow_options": {"tool": "verilator"}}}}
        assert core_target_eda_tool(doc, "sim") == "verilator"

    def test_falls_back_to_upstream_default_tool(self):
        doc = {"targets": {"syn": {"default_tool": "yosys"}}}
        assert core_target_eda_tool(doc, "syn") == "yosys"

    def test_none_when_target_absent_or_without_eda_tool(self):
        assert core_target_eda_tool({"targets": {"sim": {}}}, "sim") is None
        assert core_target_eda_tool({"targets": {}}, "missing") is None


# ---------------------------------------------------------------------------
# available_targets / target_eda_tools / resolve_target_selection (decision 10/11)
# ---------------------------------------------------------------------------


class TestAvailableTargets:
    def test_enumerates_core_targets_sorted(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        assert available_targets(tmp_path) == ["sim"]

    def test_empty_when_no_core(self, tmp_path: Path):
        """No .core authored → no selectable Targets (the legacy configs.toml
        fallback was removed with decision 23's retirement)."""
        assert available_targets(tmp_path) == []

    def test_target_eda_tools_maps_name_to_eda_tool(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        assert target_eda_tools(tmp_path) == {"sim": "verilator"}

    def test_doctor_selftest_is_resolvable_but_not_public(self, tmp_path: Path, monkeypatch):
        (tmp_path / "doctor.core").write_text(
            "CAPI=2:\nname: ::doctor:0\ntargets:\n"
            "  lint_selftest_bad:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator, booley: {doctor_selftest: true}}\n",
            encoding="utf-8",
        )

        ref = resolve_ref(tmp_path, "lint_selftest_bad")
        assert ref.doctor_selftest
        assert available_targets(tmp_path) == []
        assert target_eda_tools(tmp_path) == {}
        with pytest.raises(UnknownTargetError, match="Unknown target"):
            resolve_target_selection("lint_selftest_bad", tmp_path)

        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        assert resolve_target_selection("lint_selftest_bad", tmp_path) == ["lint_selftest_bad"]


class TestDoctorTargetMetadata:
    def test_selects_only_explicitly_marked_targets(self, tmp_path: Path):
        core = tmp_path / "doctor.core"
        core.write_text(
            "CAPI=2:\nname: ::doctor:0\ntargets:\n"
            "  sim_fast:\n"
            "    flow: sim\n"
            "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
            "  sim_manual: {flow: sim, flow_options: {tool: verilator}}\n"
            "  synth_full:\n"
            "    flow: generic\n"
            "    flow_options: {tool: yosys, booley: {doctor: [synth]}}\n",
            encoding="utf-8",
        )

        doc = read_core(core)
        assert core_target_doctor_flows(doc, "sim_fast") == ("sim",)
        assert core_target_doctor_flows(doc, "sim_manual") == ()
        assert doctor_target_selectors(tmp_path, "sim") == ["sim_fast"]
        assert doctor_target_seed(tmp_path) == ["sim_fast", "synth_full"]

    def test_doctor_selftest_metadata_is_independent_of_smoke_selection(self, tmp_path: Path):
        core = tmp_path / "selftest.core"
        core.write_text(
            "CAPI=2:\nname: ::selftest:0\ntargets:\n"
            "  lint_selftest_bad:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator, booley: {doctor_selftest: true}}\n",
            encoding="utf-8",
        )

        doc = read_core(core)
        assert core_schema_errors(core) == []
        assert core_target_is_doctor_selftest(doc, "lint_selftest_bad")
        assert core_target_doctor_flows(doc, "lint_selftest_bad") == ()

    def test_schema_requires_doctor_selftests_in_a_dedicated_core(self, tmp_path: Path):
        core = tmp_path / "mixed.core"
        core.write_text(
            "CAPI=2:\nname: ::mixed:0\ntargets:\n"
            "  lint:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator}\n"
            "  lint_selftest_bad:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator, booley: {doctor_selftest: true}}\n",
            encoding="utf-8",
        )

        assert any("dedicated .core" in error for error in core_schema_errors(core))

    @pytest.mark.parametrize(
        "metadata,needle",
        [
            ("booley: sim", "booley must be a mapping"),
            ("booley: {doctor: sim}", "doctor must be an array"),
            ("booley: {doctor: [fpga]}", "invalid Flow values"),
            ("booley: {doctor: [sim, sim]}", "must not contain duplicates"),
            ("booley: {doctor_selftest: bad}", "doctor_selftest must be a boolean"),
            ("booley: {doctor: [sim], mystery: true}", "mystery is not a supported"),
        ],
    )
    def test_schema_rejects_invalid_metadata(self, tmp_path: Path, metadata: str, needle: str):
        core = tmp_path / "invalid.core"
        core.write_text(
            "CAPI=2:\nname: ::invalid:0\ntargets:\n"
            "  sim:\n    flow: sim\n    flow_options:\n"
            f"      {metadata}\n",
            encoding="utf-8",
        )
        assert any(needle in error for error in core_schema_errors(core))


class TestResolveConfigSelection:
    def test_validates_named_targets(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        assert resolve_target_selection("sim", tmp_path) == ["sim"]

    def test_unknown_target_raises(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        with pytest.raises(UnknownTargetError, match="Unknown target"):
            resolve_target_selection("nope", tmp_path)

    def test_ambiguous_bare_token_raises(self, tmp_path: Path):
        # A bare name declared by >1 distinct core is ambiguous at selection.
        _write_core(tmp_path / "a")
        _write_core(tmp_path / "b", _CORE_TEXT.replace("::demo_core:0", "::other:0"))
        with pytest.raises(AmbiguousTargetError):
            resolve_target_selection("sim", tmp_path)
        # ...but a vlnv#name token disambiguates and passes through verbatim.
        assert resolve_target_selection("demo_core#sim", tmp_path) == ["demo_core#sim"]

    def test_empty_returns_nothing(self, tmp_path: Path):
        # ADR 0030: empty selection is [] unconditionally — no enumerate-all
        # fallback (a Flow with no --target and no configured Target refuses).
        _write_core(tmp_path / "ip")
        assert resolve_target_selection("", tmp_path) == []

    def test_flow_compatible_target_is_selectable_for_execution(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        assert resolve_target_selection("sim", tmp_path, for_flow="sim") == ["sim"]

    def test_flow_incompatible_target_is_rejected_before_execution(self, tmp_path: Path):
        core = _CORE_TEXT.replace(
            "  sim:\n",
            "  synth:\n"
            "    default_tool: yosys\n"
            "    flow: generic\n"
            "    flow_options: {tool: yosys}\n"
            "    filesets: [rtl]\n"
            "    toplevel: counter\n"
            "  sim:\n",
        )
        _write_core(tmp_path / "ip", core)

        with pytest.raises(IncompatibleTargetError, match=r"booley targets --for-flow sim"):
            resolve_target_selection("synth", tmp_path, for_flow="sim")

    def test_no_core_rejects_any_token(self, tmp_path: Path):
        """ADR 0039: a resolvable .core Target is a precondition — the old
        zero-.core transitional skip (raw tokens passed through unvalidated)
        is gone, so a project with no .core rejects every selection instead
        of silently accepting names nothing can resolve."""
        with pytest.raises(UnknownTargetError):
            resolve_target_selection("anything", tmp_path)


# ---------------------------------------------------------------------------
# resolve_ref — bare-unique / vlnv#name / shortest-unambiguous VLNV (ADR 0030)
# ---------------------------------------------------------------------------


def _min_core(vlnv: str, targets: list[str]) -> str:
    """A minimal .core declaring *vlnv* and each name in *targets* (no sources)."""
    body = "".join(
        f"  {t}:\n    flow: lint\n    flow_options: {{tool: verilator}}\n    filesets: [rtl]\n"
        for t in targets
    )
    return f"CAPI=2:\nname: {vlnv}\nfilesets:\n  rtl: {{files: []}}\ntargets:\n" + body


class TestResolveRef:
    """resolve_ref disambiguation surface: the crux of ADR 0030."""

    @staticmethod
    def _multi(root: Path) -> None:
        # Two cores share 'lint' (ambiguous); only ibex declares 'sim' (unique).
        _write_core(
            root / "ibex",
            _min_core("lowrisc:ibex:ibex_top:0.1", ["lint", "sim"]),
            create_sources=False,
        )
        _write_core(
            root / "prim",
            _min_core("lowrisc:prim:lfsr:0", ["lint"]),
            create_sources=False,
        )

    def test_bare_unique_resolves(self, tmp_path: Path):
        self._multi(tmp_path)
        ref = resolve_ref(tmp_path, "sim")
        assert ref.name == "sim"
        assert ref.vlnv == "lowrisc:ibex:ibex_top:0.1"

    def test_bare_ambiguous_lists_candidate_vlnvs(self, tmp_path: Path):
        self._multi(tmp_path)
        with pytest.raises(AmbiguousTargetError) as exc:
            resolve_ref(tmp_path, "lint")
        msg = str(exc.value)
        # The message names every candidate VLNV so the user can qualify.
        assert "lowrisc:ibex:ibex_top:0.1" in msg
        assert "lowrisc:prim:lfsr:0" in msg

    def test_full_vlnv_qualifier_resolves(self, tmp_path: Path):
        self._multi(tmp_path)
        ref = resolve_ref(tmp_path, "lowrisc:ibex:ibex_top#lint")
        assert ref.vlnv == "lowrisc:ibex:ibex_top:0.1"

    def test_shortened_library_name_qualifier_resolves(self, tmp_path: Path):
        self._multi(tmp_path)
        ref = resolve_ref(tmp_path, "ibex:ibex_top#lint")
        assert ref.vlnv == "lowrisc:ibex:ibex_top:0.1"

    def test_name_only_qualifier_resolves(self, tmp_path: Path):
        self._multi(tmp_path)
        # The shortest form — the VLNV name segment alone — resolves when unique.
        ref = resolve_ref(tmp_path, "ibex_top#lint")
        assert ref.vlnv == "lowrisc:ibex:ibex_top:0.1"

    def test_too_short_qualifier_matching_two_cores_is_ambiguous(self, tmp_path: Path):
        # Two distinct cores share the VLNV name segment 'ibex_top' and 'lint'.
        _write_core(
            tmp_path / "a",
            _min_core("lowrisc:ibex:ibex_top:0", ["lint"]),
            create_sources=False,
        )
        _write_core(
            tmp_path / "b",
            _min_core("acme:other:ibex_top:0", ["lint"]),
            create_sources=False,
        )
        with pytest.raises(AmbiguousTargetError, match="matches 2 cores"):
            resolve_ref(tmp_path, "ibex_top#lint")
        # A longer (library-qualified) form disambiguates.
        assert resolve_ref(tmp_path, "ibex:ibex_top#lint").vlnv == ("lowrisc:ibex:ibex_top:0")

    def test_unknown_name_raises_unknown(self, tmp_path: Path):
        self._multi(tmp_path)
        with pytest.raises(UnknownTargetError, match="Unknown target"):
            resolve_ref(tmp_path, "nope")

    def test_qualifier_naming_no_core_raises_unknown(self, tmp_path: Path):
        # The name exists ('lint') but no core matches the qualifier → Unknown,
        # not Ambiguous (nothing to disambiguate between).
        self._multi(tmp_path)
        with pytest.raises(UnknownTargetError, match="no Target 'lint'"):
            resolve_ref(tmp_path, "zzz#lint")

    def test_same_vlnv_two_versions_collapse_to_first(self, tmp_path: Path):
        """Two *versions* of one logical core (same vendor:library:name) collapse
        to the first discovered — not a distinct-core ambiguity (ADR 0030)."""
        _write_core(tmp_path / "v0")  # ::demo_core:0 declares 'sim'
        _write_core(tmp_path / "v1", _CORE_TEXT.replace("::demo_core:0", "::demo_core:1"))
        refs = enumerate_targets(tmp_path)
        assert set(refs) == {"sim"}
        # One logical core → a bare name is NOT ambiguous; it resolves.
        assert resolve_ref(tmp_path, "sim").vlnv.startswith("::demo_core:")
        # _enumerate_all also collapses the two versions to a single entry.
        assert len(_enumerate_all(tmp_path)["sim"]) == 1


# ---------------------------------------------------------------------------
# parse_edam — RTL/TB partition by tag (decision 13)
# ---------------------------------------------------------------------------


class TestParseEdam:
    def _resolved(self, tmp_path: Path) -> ResolvedTarget:
        edam = tmp_path / "demo_core_0.eda.yml"
        edam.write_text(_EDAM_TEXT, encoding="utf-8")
        return parse_edam(edam, target="sim", vlnv="::demo_core:0")

    def test_core_fields(self, tmp_path: Path):
        r = self._resolved(tmp_path)
        assert r.name == "sim"
        assert r.vlnv == "::demo_core:0"
        assert r.toplevel == "tb_counter"
        assert r.eda_tool == "verilator"
        assert r.flow_options == {"tool": "verilator", "flatten": False}
        assert r.build_root == tmp_path
        assert "TESTID" in r.parameters
        assert r.parameters["TESTID"]["paramtype"] == "plusarg"

    def test_rtl_tb_partition_by_tag(self, tmp_path: Path):
        r = self._resolved(tmp_path)
        assert [f.name.split("/")[-1] for f in r.rtl_files] == [
            "counter_pkg.sv",
            "counter.sv",
        ]
        assert [f.name.split("/")[-1] for f in r.tb_files] == ["tb_counter.sv"]
        assert all(not f.is_tb for f in r.rtl_files)
        assert all(f.is_tb for f in r.tb_files)

    def test_file_absolute_path(self, tmp_path: Path):
        r = self._resolved(tmp_path)
        first = r.files[0]
        assert (
            first.absolute(r.build_root)
            == (tmp_path / "src/demo_core_0/rtl/counter_pkg.sv").resolve()
        )

    def test_missing_edam_is_resolution_error(self, tmp_path: Path):
        with pytest.raises(TargetResolutionError):
            parse_edam(tmp_path / "nope.eda.yml", target="sim", vlnv="::x:0")


# ---------------------------------------------------------------------------
# parse_edam — include-file partition (asic_synthesize slice)
# ---------------------------------------------------------------------------

_EDAM_WITH_INCLUDE = textwrap.dedent(
    """\
    version: 0.2.1
    name: syn_demo_0
    toplevel: dut
    flow_options:
      tool: yosys
    files:
    - file_type: systemVerilogSource
      is_include_file: true
      name: src/syn_demo_0/rtl/include/defs.svh
      core: '::syn_demo:0'
    - file_type: systemVerilogSource
      is_include_file: true
      name: src/syn_demo_0/rtl/include/macros.svh
      core: '::syn_demo:0'
    - file_type: systemVerilogSource
      name: src/syn_demo_0/rtl/pkg.sv
      core: '::syn_demo:0'
    - file_type: systemVerilogSource
      name: src/syn_demo_0/rtl/dut.sv
      core: '::syn_demo:0'
    - file_type: systemVerilogSource
      tags:
      - tb
      name: src/syn_demo_0/tb/tb_dut.sv
      core: '::syn_demo:0'
    """
)


class TestIncludePartition:
    def _resolved(self, tmp_path: Path):
        edam = tmp_path / "syn_demo_0.eda.yml"
        edam.write_text(_EDAM_WITH_INCLUDE, encoding="utf-8")
        return parse_edam(edam, target="syn", vlnv="::syn_demo:0")

    def test_is_include_flag_parsed(self, tmp_path: Path):
        r = self._resolved(tmp_path)
        by_name = {f.name.split("/")[-1]: f for f in r.files}
        assert by_name["defs.svh"].is_include is True
        assert by_name["dut.sv"].is_include is False

    def test_rtl_source_files_exclude_includes(self, tmp_path: Path):
        r = self._resolved(tmp_path)
        # rtl_files keeps headers; rtl_source_files (the compiled set) drops them.
        assert [f.name.split("/")[-1] for f in r.rtl_files] == [
            "defs.svh",
            "macros.svh",
            "pkg.sv",
            "dut.sv",
        ]
        assert [f.name.split("/")[-1] for f in r.rtl_source_files] == [
            "pkg.sv",
            "dut.sv",
        ]

    def test_rtl_include_dirs_deduped_and_absolute(self, tmp_path: Path):
        r = self._resolved(tmp_path)
        # Two headers share one dir → a single de-duplicated, absolute include dir.
        assert r.rtl_include_dirs == ((tmp_path / "src/syn_demo_0/rtl/include").resolve(),)

    def test_tb_include_excluded_from_synth_views(self, tmp_path: Path):
        """A tb-tagged include would belong to neither synth source nor include dirs."""
        edam_text = _EDAM_WITH_INCLUDE + textwrap.dedent(
            """\
            - file_type: systemVerilogSource
              is_include_file: true
              tags:
              - tb
              name: src/syn_demo_0/tb/tb_defs.svh
              core: '::syn_demo:0'
            """
        )
        edam = tmp_path / "syn_demo_0.eda.yml"
        edam.write_text(edam_text, encoding="utf-8")
        r = parse_edam(edam, target="syn", vlnv="::syn_demo:0")
        # tb_defs.svh is tb-tagged → not a synth source, and its tb/ dir is not a
        # synth include dir (only the rtl/include dir survives).
        assert all("tb_defs" not in f.name for f in r.rtl_source_files)
        assert r.rtl_include_dirs == ((tmp_path / "src/syn_demo_0/rtl/include").resolve(),)

    def test_sdc_files_surfaces_sdc_fileset_excluding_tb(self, tmp_path: Path):
        """ADR 0029: file_type:SDC files (non-tb) surface via sdc_files; a
        tb-tagged SDC is excluded, and SDCs never leak into the RTL source set."""
        edam_text = _EDAM_WITH_INCLUDE + textwrap.dedent(
            """\
            - file_type: SDC
              name: src/syn_demo_0/sdc/constraints.sdc
              core: '::syn_demo:0'
            - file_type: SDC
              tags:
              - tb
              name: src/syn_demo_0/tb/tb.sdc
              core: '::syn_demo:0'
            """
        )
        edam = tmp_path / "syn_demo_0.eda.yml"
        edam.write_text(edam_text, encoding="utf-8")
        r = parse_edam(edam, target="syn", vlnv="::syn_demo:0")
        sdc_names = [f.name.split("/")[-1] for f in r.sdc_files]
        assert sdc_names == ["constraints.sdc"]  # tb.sdc excluded
        # An SDC is not (System)Verilog, so it must not reach the HDL source set.
        assert all(not f.name.endswith(".sdc") for f in r.rtl_hdl_source_files)


# ---------------------------------------------------------------------------
# resolve_target — CLI invocation (mocked) + command shape
# ---------------------------------------------------------------------------


class TestResolveTargetMocked:
    def test_builds_expected_cli_and_parses_edam(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")
        build_root = tmp_path / "build"
        captured: dict = {}

        def fake_runner(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            # Emulate fusesoc laying out <build>/<name>/<target>/<name>.eda.yml.
            out = build_root / "demo_core_0" / "sim"
            out.mkdir(parents=True, exist_ok=True)
            (out / "demo_core_0.eda.yml").write_text(_EDAM_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        result = resolve_target(
            "sim",
            project_root=project,
            build_root=build_root,
            runner=fake_runner,
        )
        assert result.toplevel == "tb_counter"
        assert result.eda_tool == "verilator"
        # CLI shape: cores-root at project, build-root isolated, --setup, target, vlnv.
        cmd = captured["cmd"]
        assert cmd[: len(DEFAULT_FUSESOC_CMD)] == list(DEFAULT_FUSESOC_CMD)
        assert "--cores-root" in cmd and str(project) in cmd
        assert "--build-root" in cmd and str(build_root) in cmd
        assert "--setup" in cmd
        assert cmd[cmd.index("--target") + 1] == "sim"
        assert cmd[-1] == "::demo_core:0"  # VLNV is the positional core spec
        assert captured["cwd"] == str(project)

    def test_unknown_target_raises_before_invoking(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        def boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("runner should not run for an unknown target")

        with pytest.raises(TargetResolutionError, match="Unknown target"):
            resolve_target(
                "nope",
                project_root=project,
                build_root=tmp_path / "build",
                runner=boom,
            )

    def test_nonzero_exit_raises_with_stderr(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        with pytest.raises(TargetResolutionError, match="boom"):
            resolve_target(
                "sim",
                project_root=project,
                build_root=tmp_path / "build",
                vlnv="::demo_core:0",
                runner=failing,
            )

    def test_missing_fusesoc_binary_raises(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        def missing(*a, **k):
            raise FileNotFoundError("fusesoc")

        with pytest.raises(TargetResolutionError, match="could not invoke"):
            resolve_target(
                "sim",
                project_root=project,
                build_root=tmp_path / "build",
                vlnv="::demo_core:0",
                runner=missing,
            )


class TestMissingSourcePreflight:
    """resolve_target fails fast when a Target's fileset paths don't exist."""

    # A core whose sim target references a baseline under worktrees/ (the
    # motivating incident: the user is expected to `git worktree add` it).
    _BASELINE_CORE = textwrap.dedent(
        """\
        CAPI=2:
        name: ::baseline_core:0

        filesets:
          rtl:
            files:
              - rtl/top.sv: {file_type: systemVerilogSource}
              - worktrees/scalar_1bfe1733/rtl/alu.sv: {file_type: systemVerilogSource}
              - worktrees/scalar_1bfe1733/rtl/mul.sv: {file_type: systemVerilogSource}

        targets:
          sim_base:
            flow: sim
            flow_options: {tool: verilator}
            filesets: [rtl]
            toplevel: top
        """
    )

    def test_lists_all_missing_paths_and_never_invokes_fusesoc(self, tmp_path: Path):
        core = _write_core(tmp_path, self._BASELINE_CORE, create_sources=False)
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "top.sv").touch()  # exists → not reported

        def boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("runner must not run when sources are missing")

        with pytest.raises(MissingSourceError) as exc_info:
            resolve_target(
                "sim_base",
                project_root=tmp_path,
                build_root=tmp_path / "build",
                runner=boom,
            )
        msg = str(exc_info.value)
        # Header names the target, the count, and the declaring core.
        assert "cannot resolve Target 'sim_base'" in msg
        assert "2 source path(s)" in msg
        assert str(core) in msg
        # ALL missing paths are listed, not just the first.
        assert "  - worktrees/scalar_1bfe1733/rtl/alu.sv" in msg
        assert "  - worktrees/scalar_1bfe1733/rtl/mul.sv" in msg
        assert "top.sv" not in msg  # the existing file is not reported
        # One worktree hint, deduped across the two files under the same root.
        assert msg.count("git worktree add") == 1
        assert (
            "hint: 'worktrees/scalar_1bfe1733' looks like a git worktree "
            "baseline; create it with: git worktree add "
            "worktrees/scalar_1bfe1733 <commit-ish>"
        ) in msg

    def test_missing_source_is_a_target_resolution_error(self):
        # Callers' existing `except TargetResolutionError` handlers must catch it.
        assert issubclass(MissingSourceError, TargetResolutionError)

    def test_no_worktree_hint_for_plain_missing_paths(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::plain_core:0
            filesets:
              rtl:
                files:
                  - rtl/gone.sv: {file_type: systemVerilogSource}
            targets:
              sim_plain:
                flow_options: {tool: verilator}
                filesets: [rtl]
                toplevel: top
            """
        )
        _write_core(tmp_path, core, create_sources=False)
        with pytest.raises(MissingSourceError) as exc_info:
            preflight_target_sources("sim_plain", tmp_path)
        msg = str(exc_info.value)
        assert "  - rtl/gone.sv" in msg
        assert "git worktree add" not in msg

    def test_passes_when_all_sources_exist(self, tmp_path: Path):
        _write_core(tmp_path)  # default fixture creates the declared sources
        preflight_target_sources("sim", tmp_path)  # must not raise
        assert missing_target_sources(tmp_path, "sim") == []

    def test_paths_resolve_relative_to_core_dir_not_project_root(self, tmp_path: Path):
        # Core nested under ip/: its rtl/ lives beside the .core, not at root.
        _write_core(tmp_path / "ip")
        assert missing_target_sources(tmp_path, "sim") == []

    def test_glob_and_conditional_entries_are_not_hard_failed(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::globby_core:0
            filesets:
              rtl:
                files:
                  - rtl/real.sv: {file_type: systemVerilogSource}
                  - rtl/*.svh: {file_type: systemVerilogSource}
            targets:
              sim_glob:
                flow_options: {tool: verilator}
                filesets: [rtl, "tool_verilator ? (cond_fs)"]
                toplevel: top
            """
        )
        _write_core(tmp_path, core, create_sources=False)
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "real.sv").touch()
        # Only the literal path is checked; the glob and the conditional
        # fileset are left for FuseSoC itself to judge.
        assert missing_target_sources(tmp_path, "sim_glob") == []

    def test_unknown_target_is_not_this_preflights_error(self, tmp_path: Path):
        _write_core(tmp_path)
        # Unknown / unenumerable targets are someone else's diagnostic.
        assert missing_target_sources(tmp_path, "nope") == []

    def test_empty_project_is_silent(self, tmp_path: Path):
        assert missing_target_sources(tmp_path, "anything") == []
        preflight_target_sources("anything", tmp_path)  # must not raise


class TestTryResolveTarget:
    """The soft, transitional bridge: prefer the .core, else None for fallback."""

    def _default_build_root(self, project: Path, target: str) -> Path:
        return project / ".booley_project" / ".runtime" / "edalize" / "payload" / target

    def test_none_when_no_core_declares_config(self, tmp_path: Path):
        # An empty project (no .core) → None, and the runner is never invoked.
        def boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("must not resolve when no .core declares the config")

        assert try_resolve_target("sim", project_root=tmp_path / "proj", runner=boom) is None

    def test_returns_resolved_when_core_and_setup_succeed(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        def fake_runner(cmd, **kwargs):
            out = self._default_build_root(project, "sim") / "demo_core_0" / "sim"
            out.mkdir(parents=True, exist_ok=True)
            (out / "demo_core_0.eda.yml").write_text(_EDAM_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        resolved = try_resolve_target("sim", project_root=project, runner=fake_runner)
        assert resolved is not None
        assert resolved.toplevel == "tb_counter"
        assert resolved.vlnv == "::demo_core:0"

    def test_none_on_setup_failure(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        assert try_resolve_target("sim", project_root=project, runner=failing) is None

    def test_none_when_fusesoc_missing(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        def missing(*a, **k):
            raise FileNotFoundError("fusesoc")

        assert try_resolve_target("sim", project_root=project, runner=missing) is None


# ---------------------------------------------------------------------------
# resolve_target — real fusesoc end-to-end (gated on fusesoc being importable)
# ---------------------------------------------------------------------------


class TestResolveTargetReal:
    def test_real_setup_resolves_projected_stealth_core(self, tmp_path: Path):
        pytest.importorskip("fusesoc")
        project = tmp_path / "proj"
        (project / ".booley_project" / "cores").mkdir(parents=True)
        (project / ".booley_project" / "booley.toml").write_text(
            "[stealth]\nenabled = true\n", encoding="utf-8"
        )
        (project / ".booley_project" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        (project / "rtl").mkdir()
        (project / "tb").mkdir()
        (project / "rtl" / "counter_pkg.sv").write_text(
            "package counter_pkg; parameter int W=8; endpackage\n", encoding="utf-8"
        )
        (project / "rtl" / "counter.sv").write_text(
            "module counter(input logic clk); endmodule\n", encoding="utf-8"
        )
        (project / "tb" / "tb_counter.sv").write_text(
            "module tb_counter; counter dut(.clk(1'b0)); endmodule\n", encoding="utf-8"
        )
        _write_core(project / ".booley_project" / "cores", create_sources=False)
        cmd = (
            list(DEFAULT_FUSESOC_CMD)
            if shutil.which("fusesoc")
            else [sys.executable, "-c", "from fusesoc.main import main; main()"]
        )

        resolved = resolve_target(
            "sim",
            project_root=project,
            build_root=project / "out",
            fusesoc_cmd=cmd,
        )

        assert resolved.toplevel == "tb_counter"
        assert (project / ".booley-projected-design.core").is_file()

    def test_real_setup_ignores_invalid_native_core(self, tmp_path: Path):
        pytest.importorskip("fusesoc")
        project = tmp_path / "proj"
        (project / ".booley_project" / "cores").mkdir(parents=True)
        (project / ".booley_project" / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        (project / "rtl").mkdir()
        (project / "tb").mkdir()
        (project / "rtl" / "counter_pkg.sv").touch()
        (project / "rtl" / "counter.sv").touch()
        (project / "tb" / "tb_counter.sv").touch()
        _write_core(project / ".booley_project" / "cores", create_sources=False)
        (project / "broken.core").write_text(
            "CAPI=2:\nname: ::broken:0\nfilesets:\n  bad:\n    files: []\n    depend:\n",
            encoding="utf-8",
        )
        cmd = (
            list(DEFAULT_FUSESOC_CMD)
            if shutil.which("fusesoc")
            else [sys.executable, "-c", "from fusesoc.main import main; main()"]
        )

        resolved = resolve_target(
            "sim",
            project_root=project,
            build_root=project / "out",
            fusesoc_cmd=cmd,
        )

        assert resolved.toplevel == "tb_counter"

    def test_real_setup_resolves_edam(self, tmp_path: Path):
        pytest.importorskip("fusesoc")
        # Build a runnable project on disk.
        project = tmp_path / "proj"
        (project / "rtl").mkdir(parents=True)
        (project / "tb").mkdir(parents=True)
        (project / "rtl" / "counter_pkg.sv").write_text(
            "package counter_pkg; parameter int W=8; endpackage\n", encoding="utf-8"
        )
        (project / "rtl" / "counter.sv").write_text(
            "module counter(input logic clk); endmodule\n", encoding="utf-8"
        )
        (project / "tb" / "tb_counter.sv").write_text(
            "module tb_counter; counter dut(.clk(1'b0)); endmodule\n",
            encoding="utf-8",
        )
        _write_core(project)

        # Prefer the console script; otherwise invoke the importable module.
        if shutil.which("fusesoc"):
            cmd = list(DEFAULT_FUSESOC_CMD)
        else:
            cmd = [sys.executable, "-c", "from fusesoc.main import main; main()"]

        result = resolve_target(
            "sim",
            project_root=project,
            build_root=tmp_path / "build",
            fusesoc_cmd=cmd,
        )
        assert result.toplevel == "tb_counter"
        assert result.eda_tool == "verilator"
        assert [f.name.split("/")[-1] for f in result.tb_files] == ["tb_counter.sv"]
        assert len(result.rtl_files) == 2
        assert (result.build_root / "Makefile").exists()


# ---------------------------------------------------------------------------
# target_source_files — pre-resolve RTL/TB partition from the .core (dec 13)
# ---------------------------------------------------------------------------


class TestTargetSourceFiles:
    def test_partitions_sim_target_by_tb_tag(self, tmp_path: Path):
        _write_core(tmp_path)
        src = target_source_files(tmp_path, "sim")
        assert src.rtl_source_files == (
            "rtl/counter_pkg.sv",
            "rtl/counter.sv",
        )
        assert src.tb_files == ("tb/tb_counter.sv",)

    def test_rtl_only_target_has_no_tb(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::syn_demo:0
            filesets:
              rtl:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
            targets:
              synth:
                default_tool: yosys
                filesets: [rtl]
                toplevel: dut
            """
        )
        _write_core(tmp_path, core)
        src = target_source_files(tmp_path, "synth")
        assert src.tb_files == ()
        assert src.rtl_source_files == ("rtl/dut.sv",)

    def test_unknown_target_raises(self, tmp_path: Path):
        _write_core(tmp_path)
        with pytest.raises(UnknownTargetError):
            target_source_files(tmp_path, "nope")

    def test_ref_partition_bypasses_selector_rescan(self, tmp_path: Path, monkeypatch):
        from booley.fusesoc import fusesoc_registry

        _write_core(tmp_path / "a")
        _write_core(
            tmp_path / "b",
            _CORE_TEXT.replace("::demo_core:0", "::other_core:0"),
        )
        refs = _enumerate_all(tmp_path)["sim"]

        with pytest.raises(AmbiguousTargetError):
            target_source_files(tmp_path, "sim")
        qualified = target_source_files(tmp_path, "other_core#sim")

        monkeypatch.setattr(
            fusesoc_registry,
            "_enumerate_all",
            lambda _root: pytest.fail("ref-based partition rescanned the registry"),
        )

        assert target_source_files_for_ref(tmp_path, refs[1]) == qualified


class TestTargetSourceFilesDependencyClosure:
    """F-27: the DUT commonly arrives through a dependency, not the root core.

    Ibex's sim Target's root fileset owns the C++/firmware harness while
    `ibex_top_tracing.sv` is contributed transitively, so a root-core-only
    read returned no RTL at all and mutation_tester failed before injection.
    """

    def _layered_repo(self, tmp_path: Path) -> None:
        harness = textwrap.dedent(
            """\
            CAPI=2:
            name: acme:demo:port:0
            filesets:
              harness:
                files:
                  - tb/harness.cpp: {file_type: cppSource}
                  - tb/tb_top.sv: {file_type: systemVerilogSource, tags: [tb]}
                depend: [acme:demo:core]
            targets:
              sim:
                default_tool: verilator
                filesets: [harness]
                toplevel: tb_top
            """
        )
        _write_core(tmp_path, harness)
        dut = textwrap.dedent(
            """\
            CAPI=2:
            name: acme:demo:core:0
            filesets:
              rtl:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
                  - rtl/dut_pkg.sv: {file_type: systemVerilogSource}
            targets:
              default:
                filesets: [rtl]
            """
        )
        dep_dir = tmp_path / "vendor" / "core"
        dep_dir.mkdir(parents=True)
        dep_core = dep_dir / "dut.core"
        dep_core.write_text(dut, encoding="utf-8")
        _touch_declared_sources(dep_core)

    def test_root_only_read_misses_the_transitive_dut(self, tmp_path: Path):
        """The default stays root-only — this documents why that is not enough."""
        self._layered_repo(tmp_path)
        src = target_source_files(tmp_path, "sim")
        assert src.rtl_source_files == ("tb/harness.cpp",)

    def test_closure_read_finds_the_transitive_dut(self, tmp_path: Path):
        self._layered_repo(tmp_path)
        src = target_source_files(tmp_path, "sim", include_dependencies=True)
        assert "vendor/core/rtl/dut.sv" in src.rtl_source_files
        assert "vendor/core/rtl/dut_pkg.sv" in src.rtl_source_files
        # The root core's own files are still first, and TB tagging survives.
        assert src.rtl_source_files[0] == "tb/harness.cpp"
        assert src.tb_files == ("tb/tb_top.sv",)

    def test_closure_read_does_not_duplicate_shared_dependencies(self, tmp_path: Path):
        """A file reachable twice must not be mutated or counted twice."""
        self._layered_repo(tmp_path)
        src = target_source_files(tmp_path, "sim", include_dependencies=True)
        assert len(src.rtl_source_files) == len(set(src.rtl_source_files))

    def test_unresolvable_dependency_does_not_break_the_read(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: acme:demo:port:0
            filesets:
              harness:
                files:
                  - rtl/top.sv: {file_type: systemVerilogSource}
                depend: [acme:demo:absent]
            targets:
              sim:
                filesets: [harness]
                toplevel: top
            """
        )
        _write_core(tmp_path, core)
        src = target_source_files(tmp_path, "sim", include_dependencies=True)
        assert src.rtl_source_files == ("rtl/top.sv",)

    def test_excludes_include_headers_from_rtl_sources(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::inc_demo:0
            filesets:
              rtl:
                files:
                  - rtl/defs.svh: {file_type: systemVerilogSource, is_include_file: true}
                  - rtl/dut.sv: {file_type: systemVerilogSource}
              tb:
                files:
                  - tb/tb_dut.sv: {file_type: systemVerilogSource}
                tags: [tb]
            targets:
              sim:
                filesets: [rtl, tb]
                toplevel: tb_dut
            """
        )
        _write_core(tmp_path, core)
        src = target_source_files(tmp_path, "sim")
        # The include header is not a compiled source; the tb file is tb-tagged.
        assert src.rtl_source_files == ("rtl/dut.sv",)
        assert src.tb_files == ("tb/tb_dut.sv",)

    def test_per_file_tb_tag_in_mixed_fileset(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::mixed_demo:0
            filesets:
              src:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
                  - tb/tb_dut.sv: {file_type: systemVerilogSource, tags: [tb]}
            targets:
              sim:
                filesets: [src]
                toplevel: tb_dut
            """
        )
        _write_core(tmp_path, core)
        src = target_source_files(tmp_path, "sim")
        assert src.rtl_source_files == ("rtl/dut.sv",)
        assert src.tb_files == ("tb/tb_dut.sv",)


class TestSimTargetHasUntaggedTb:
    def test_false_when_tb_is_tagged(self, tmp_path: Path):
        _write_core(tmp_path)  # sim target's tb fileset carries tags: [tb]
        assert sim_target_has_untagged_tb(tmp_path, "sim") is False

    def test_true_when_tb_fileset_untagged(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::untagged_demo:0
            filesets:
              rtl:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
              tb:
                files:
                  - tb/tb_dut.sv: {file_type: systemVerilogSource}
            targets:
              sim:
                filesets: [rtl, tb]
                toplevel: tb_dut
            """
        )
        _write_core(tmp_path, core)
        # tb fileset has no tags:[tb] → its file mis-classifies as RTL.
        src = target_source_files(tmp_path, "sim")
        assert "tb/tb_dut.sv" in src.rtl_source_files
        assert src.tb_files == ()
        assert sim_target_has_untagged_tb(tmp_path, "sim") is True


# ---------------------------------------------------------------------------
# write_trace_overlay / trace_overlay_vlnv  (the --trace overlay slice)
# ---------------------------------------------------------------------------


class TestTraceOverlayVlnv:
    @pytest.mark.parametrize(
        "base, expected",
        [
            ("::design:0", "::design-booleytrace:0"),
            ("vend:lib:core:1.2", "vend:lib:core-booleytrace:1.2"),
            ("solo", "solo-booleytrace"),
        ],
    )
    def test_suffixes_name_component(self, base: str, expected: str):
        assert trace_overlay_vlnv(base) == expected


class TestWriteTraceOverlay:
    def test_writes_colocated_overlay_with_trace_options(self, tmp_path: Path):
        base = _write_core(tmp_path / "ip")  # sim target: verilator, flow sim
        base_vlnv = read_core(base)["name"]
        expected_vlnv = trace_overlay_vlnv(base_vlnv)
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            # Co-located with the base .core so relative fileset paths still resolve.
            assert overlay.core_file.parent == base.parent
            assert TRACE_OVERLAY_MARKER in overlay.core_file.name
            assert overlay.vlnv == expected_vlnv
            assert overlay.vlnv != base_vlnv  # distinct → its own build root

            doc = read_core(overlay.core_file)
            assert doc["name"] == expected_vlnv
            opts = doc["targets"]["sim"]["flow_options"]["verilator_options"]
            assert "--trace" in opts
            assert opts[opts.index("--trace-depth") + 1] == "99"
            # The base Target is untouched (agent-immutable).
            assert "--trace" not in read_core(base)["targets"]["sim"].get("flow_options", {}).get(
                "verilator_options", []
            )
        finally:
            overlay.cleanup()

    def test_overlay_is_skipped_by_discovery(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            # The overlay .core exists on disk beside the base...
            assert overlay.core_file.exists()
            # ...but Booley's enumeration ignores it, so it never pollutes the
            # selectable Target list (no `sim` collision, no overlay Target).
            found = discover_cores(tmp_path)
            assert overlay.core_file not in found
            assert set(enumerate_targets(tmp_path)) == {"sim"}
        finally:
            overlay.cleanup()

    def test_preserves_authored_vcd_recipe(self, tmp_path: Path):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            "    flow_options:\n      tool: verilator\n"
            "      verilator_options: [--timing, --trace, --trace-depth, '5']\n",
        )
        assert "verilator_options" in core  # guard: the replace actually matched
        _write_core(tmp_path / "ip", core)
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            opts = read_core(overlay.core_file)["targets"]["sim"]["flow_options"][
                "verilator_options"
            ]
            # An authored recipe is one contract: Booley must not rewrite its
            # format or depth while leaving the project's harness untouched.
            assert opts.count("--trace") == 1
            assert opts.count("--trace-depth") == 1
            assert opts[opts.index("--trace-depth") + 1] == "5"
            assert "--timing" in opts  # non-trace options preserved
            assert overlay.mode is TraceMode.VCD_FIFO
        finally:
            overlay.cleanup()

    def test_preserves_authored_native_fst_recipe(self, tmp_path: Path):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            "    flow_options:\n      tool: verilator\n"
            "      verilator_options: [--timing, --trace, --trace-fst, "
            "--trace-depth, '7', -CFLAGS, -DVM_TRACE_FMT_FST]\n",
        )
        _write_core(tmp_path / "ip", core)

        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            opts = read_core(overlay.core_file)["targets"]["sim"]["flow_options"][
                "verilator_options"
            ]
            assert opts == [
                "--timing",
                "--trace",
                "--trace-fst",
                "--trace-depth",
                "7",
                "-CFLAGS",
                "-DVM_TRACE_FMT_FST",
            ]
            assert overlay.mode is TraceMode.NATIVE_FST
        finally:
            overlay.cleanup()

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ("--trace-fst, --trace-vcd", "both native FST and VCD"),
            ("--trace-saif", "SAIF tracing is not supported"),
        ],
    )
    def test_rejects_unsupported_authored_trace_recipe(
        self,
        tmp_path: Path,
        options: str,
        message: str,
    ):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            f"    flow_options:\n      tool: verilator\n      verilator_options: [{options}]\n",
        )
        _write_core(tmp_path / "ip", core)

        with pytest.raises(FuseSocError, match=message):
            write_trace_overlay("sim", project_root=tmp_path)

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            (
                "-CFLAGS, -DVM_TRACE_FMT_FST",
                "VM_TRACE_FMT_FST.*requires --trace-fst",
            ),
            (
                "--trace-vcd, -CFLAGS, -DVM_TRACE_FMT_FST",
                "FST CFLAG.*VCD trace option",
            ),
            (
                "--trace-fst, -CFLAGS, -DVM_TRACE_FMT_VCD",
                "VCD CFLAG.*native FST trace option",
            ),
            (
                "-CFLAGS, '-DVM_TRACE_FMT_FST -DVM_TRACE_FMT_VCD'",
                "both FST and VCD CFLAGS",
            ),
        ],
    )
    def test_rejects_incoherent_trace_format_cflags(
        self,
        tmp_path: Path,
        options: str,
        message: str,
    ):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            f"    flow_options:\n      tool: verilator\n      verilator_options: [{options}]\n",
        )
        _write_core(tmp_path / "ip", core)

        with pytest.raises(FuseSocError, match=message):
            write_trace_overlay("sim", project_root=tmp_path)

    def test_cleanup_is_idempotent(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        overlay.cleanup()
        assert not overlay.core_file.exists()
        overlay.cleanup()  # second call must not raise

    def test_rejects_unknown_target(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        with pytest.raises(UnknownTargetError):
            write_trace_overlay("nope", project_root=tmp_path)

    def test_rejects_non_verilator_sim_target(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::lint_demo:0
            filesets:
              rtl:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
            targets:
              lint:
                flow: lint
                flow_options:
                  tool: verilator
                filesets: [rtl]
                toplevel: dut
            """
        )
        _write_core(tmp_path / "ip", core)
        # lint flow has no testbench — the trace overlay is sim-only.
        with pytest.raises(FuseSocError):
            write_trace_overlay("lint", project_root=tmp_path)

    # --- Icarus sim trace overlay (roots the dump module, no verilator_options) -

    _ICARUS_CORE = textwrap.dedent(
        """\
        CAPI=2:
        name: ::icarus_demo:0
        filesets:
          rtl:
            files:
              - rtl/dut.sv: {file_type: systemVerilogSource}
          tb:
            files:
              - tb/tb_dut.sv: {file_type: systemVerilogSource}
              - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}
            tags: [tb]
        targets:
          default:
            filesets: [rtl]
          sim:
            default_tool: icarus
            flow: sim
            flow_options:
              tool: icarus
            filesets: [rtl, tb]
            toplevel: tb_dut
        """
    )

    def test_icarus_overlay_roots_dump_module(self, tmp_path: Path):
        _write_core(tmp_path / "ip", self._ICARUS_CORE)
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            sim = read_core(overlay.core_file)["targets"]["sim"]["flow_options"]
            # Icarus gets an explicit dump-module root (edalize's -s <top> prunes
            # the uninstantiated booley_vcd_dump otherwise) and NO verilator_options.
            assert "-sbooley_vcd_dump" in sim["iverilog_options"]
            assert "verilator_options" not in sim
        finally:
            overlay.cleanup()

    def test_icarus_overlay_root_is_idempotent(self, tmp_path: Path):
        core = self._ICARUS_CORE.replace(
            "    flow_options:\n      tool: icarus\n",
            "    flow_options:\n      tool: icarus\n"
            "      iverilog_options: [-sbooley_vcd_dump, -g2012]\n",
        )
        assert "iverilog_options" in core  # guard: the replace matched
        _write_core(tmp_path / "ip", core)
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            opts = read_core(overlay.core_file)["targets"]["sim"]["flow_options"][
                "iverilog_options"
            ]
            assert opts.count("-sbooley_vcd_dump") == 1  # no double-up
            assert "-g2012" in opts  # non-trace options preserved
        finally:
            overlay.cleanup()

    def test_icarus_overlay_injects_dump_module_when_absent(self, tmp_path: Path):
        # Stealth Mode: an Icarus sim Target whose fileset omits booley_vcd_dump
        # is NOT rejected — the overlay supplies the module from Booley's refs/
        # so the design repo needs no tracked trace source. It is still rooted.
        core = self._ICARUS_CORE.replace(
            "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n",
            "",
        )
        assert "booley_vcd_dump" not in core  # guard: the replace matched
        _write_core(tmp_path / "ip", core, create_sources=False)
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            doc = read_core(overlay.core_file)
            sim = doc["targets"]["sim"]
            # The supplied module rides in via an overlay-only fileset the target
            # pulls in, and is rooted just like an authored one.
            assert "booley_trace_dump" in sim["filesets"]
            assert "-sbooley_vcd_dump" in sim["flow_options"]["iverilog_options"]
            # It is physically supplied (ephemeral, marker-named) and tracked for
            # cleanup — nothing lands in the design's own tree by name.
            assert len(overlay.extra_files) == 1
            supplied = overlay.extra_files[0]
            assert supplied.is_file()
            assert TRACE_OVERLAY_MARKER in supplied.name
        finally:
            overlay.cleanup()
        assert not overlay.extra_files[0].exists()  # swept up with the .core

    def test_native_core_isolation_keeps_icarus_dump_source_after_cleanup(
        self,
        tmp_path: Path,
    ):
        pytest.importorskip("fusesoc")
        project = tmp_path / "project"
        cores = project / ".booley_project" / "cores"
        cores.mkdir(parents=True)
        (project / ".booley_project" / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        (project / ".booley_project" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        (project / "rtl").mkdir()
        (project / "tb").mkdir()
        (project / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (project / "tb" / "tb_dut.sv").write_text(
            "module tb_dut; dut dut(); endmodule\n",
            encoding="utf-8",
        )
        core = self._ICARUS_CORE.replace(
            "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n",
            "",
        )
        _write_core(cores, core, create_sources=False)
        overlay = write_trace_overlay("sim", project_root=project)
        try:
            resolved = resolve_target(
                "sim",
                project_root=project,
                build_root=project / "build",
                vlnv=overlay.vlnv,
            )
        finally:
            overlay.cleanup()

        dumps = [
            resolved.build_root / file.name
            for file in resolved.files
            if Path(file.name).name == "booley_vcd_dump.sv"
        ]
        assert len(dumps) == 1
        assert dumps[0].is_file()
        assert not (cores / f"booley_vcd_dump{TRACE_OVERLAY_MARKER}.sv").exists()

    def test_projected_icarus_overlay_rebases_injected_dump(self, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        (project_dir / "cores").mkdir(parents=True)
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        (project_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        core = self._ICARUS_CORE.replace(
            "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n", ""
        )
        _write_core(project_dir / "cores", core, create_sources=False)
        overlay = write_trace_overlay("sim", project_root=tmp_path)
        try:
            from booley.fusesoc.fusesoc_registry import setup_command

            setup_command(
                "sim",
                project_root=tmp_path,
                build_root=tmp_path / "build",
                vlnv=overlay.vlnv,
            )
            projected = next(path for path in overlay.extra_files if path.name.endswith(".core"))
            doc = read_core(projected)
            entry = doc["filesets"]["booley_trace_dump"]["files"][0]
            assert next(iter(entry)).startswith(".booley_project/cores/")
        finally:
            overlay.cleanup()
        assert not projected.exists()


# ---------------------------------------------------------------------------
# CAPI2 array-field schema (cheap host-side subset — the "must be array" class
# that Booley's tolerant reader accepts but FuseSoC rejects at resolution).
# ---------------------------------------------------------------------------


class TestCoreSchemaErrors:
    def test_clean_core_has_no_errors(self, tmp_path: Path):
        core = _write_core(tmp_path)
        assert core_schema_errors(core) == []

    def test_scalar_depend_flagged(self, tmp_path: Path):
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              tb:
                files:
                  - tb/tb.sv: {file_type: systemVerilogSource}
                depend: not_a_list
            targets:
              sim: {flow: sim, filesets: [tb]}
            """
        )
        core = _write_core(tmp_path, text)
        assert "filesets.tb.depend must be array" in core_schema_errors(core)

    def test_empty_depend_none_flagged(self, tmp_path: Path):
        # A bare ``depend:`` parses to None — FuseSoC still rejects it (the exact
        # upstream trap: it passed plain doctor and exploded only at --deep).
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              tb:
                files:
                  - tb/tb.sv: {file_type: systemVerilogSource}
                depend:
            targets:
              sim: {flow: sim, filesets: [tb]}
            """
        )
        core = _write_core(tmp_path, text)
        assert "filesets.tb.depend must be array" in core_schema_errors(core)

    def test_scalar_target_filesets_append_flagged(self, tmp_path: Path):
        # A scalar filesets_append passed the audit and then splat into
        # per-character fileset names ["r","t","l"] inside
        # target_fileset_names — the exact drift this table exists to stop.
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              rtl: {files: []}
            targets:
              sim: {flow: sim, filesets: [rtl], filesets_append: rtl}
            """
        )
        core = _write_core(tmp_path, text)
        assert "targets.sim.filesets_append must be array" in core_schema_errors(core)

    def test_scalar_target_filesets_flagged(self, tmp_path: Path):
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              rtl: {files: []}
            targets:
              sim: {flow: sim, filesets: rtl}
            """
        )
        core = _write_core(tmp_path, text)
        assert "targets.sim.filesets must be array" in core_schema_errors(core)

    def test_unreadable_core_reports_error(self, tmp_path: Path):
        core = tmp_path / "broken.core"
        core.write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert core_schema_errors(core)  # non-mapping .core is itself a violation

    def test_unknown_per_file_key_flagged(self, tmp_path: Path):
        # QA-3: ``vendored`` is not a CAPI2 per-file attribute; fusesoc's schema
        # (additionalProperties: false) drops the whole core. Booley's tolerant
        # reader used to greenlight it and only explode at --deep — catch it here.
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              rtl:
                files:
                  - rtl/foo.sv: {file_type: systemVerilogSource, vendored: true}
            targets:
              sim: {flow: sim, filesets: [rtl]}
            """
        )
        core = _write_core(tmp_path, text)
        errors = core_schema_errors(core)
        assert any("vendored" in e and "not a valid CAPI2 per-file key" in e for e in errors), (
            errors
        )

    def test_all_valid_per_file_keys_accepted(self, tmp_path: Path):
        # Every CAPI2-valid per-file key must pass (no false positives). Mirrors
        # fusesoc 2.4.6 json_schema.py's ``files`` $def allowlist.
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              rtl:
                files:
                  - rtl/foo.sv:
                      file_type: systemVerilogSource
                      is_include_file: false
                      include_path: rtl
                      logical_name: work
                      copyto: build/foo.sv
                      tags: [synth]
                      define: {WIDTH: 8}
            targets:
              sim: {flow: sim, filesets: [rtl]}
            """
        )
        core = _write_core(tmp_path, text)
        assert core_schema_errors(core) == []

    def test_bare_string_file_entry_not_flagged(self, tmp_path: Path):
        # A file given as a plain path string carries no attrs — nothing to reject.
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              rtl:
                files:
                  - rtl/foo.sv
            targets:
              sim: {flow: sim, filesets: [rtl]}
            """
        )
        core = _write_core(tmp_path, text)
        assert core_schema_errors(core) == []


class TestTargetSchemaFieldRegistry:
    """Golden drift-guard between the schema table and its readers.

    The array-field table (:data:`_CAPI2_TARGET_ARRAY_FIELDS`) and the
    pre-resolve readers that splat target-def lists are maintained by hand in
    two places; ``filesets_append`` once existed only on the reader side, so a
    malformed scalar passed the audit and splat into per-character fileset
    names. This test derives the consumed keys from the reader itself, so the
    two can never drift apart again.
    """

    def test_target_fileset_names_keys_are_schema_audited(self):
        from booley.fusesoc.fusesoc_registry import (
            _CAPI2_TARGET_ARRAY_FIELDS,
            target_fileset_names,
        )

        consumed: set[str] = set()

        class _Recorder(dict):
            def get(self, key, default=None):
                consumed.add(key)
                return super().get(key, default)

        # Non-empty so the reader's `target_def or {}` guard keeps the recorder.
        target_fileset_names(_Recorder({"filesets": ["rtl"]}))

        assert consumed, "reader consumed no keys — recorder wiring broken"
        missing = consumed - set(_CAPI2_TARGET_ARRAY_FIELDS)
        assert not missing, (
            f"target_fileset_names() consumes target-def keys {sorted(missing)} that "
            "core_schema_errors() does not audit as arrays — add them to "
            "_CAPI2_TARGET_ARRAY_FIELDS or a scalar will splat into per-character names"
        )


class TestCoreTargetFlowOption:
    def test_reads_arch(self, tmp_path: Path):
        doc = read_core(
            _write_core(
                tmp_path,
                textwrap.dedent(
                    """\
            CAPI=2:
            name: ::demo:0
            filesets: {rtl: {files: []}}
            targets:
              synth:
                flow: generic
                flow_options: {tool: yosys, arch: xilinx}
                filesets: [rtl]
            """
                ),
            )
        )
        assert core_target_flow_option(doc, "synth", "arch") == "xilinx"
        assert core_target_flow_option(doc, "synth", "missing") is None
        assert core_target_flow_option(doc, "nope", "arch") is None


class TestAllReferencedFiles:
    def test_lists_every_fileset_file(self, tmp_path: Path):
        _write_core(tmp_path)
        files = all_referenced_files(tmp_path)
        assert "rtl/counter_pkg.sv" in files
        assert "rtl/counter.sv" in files
        assert "tb/tb_counter.sv" in files

    def test_includes_data_files(self, tmp_path: Path):
        # A file_type: user data file (e.g. firmware hex) must be surfaced so the
        # untracked-file doctor check can see it.
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              tb:
                files:
                  - tb/tb.sv: {file_type: systemVerilogSource}
                  - firmware/firmware.hex: {file_type: user}
                tags: [tb]
            targets:
              sim: {flow: sim, filesets: [tb]}
            """
        )
        _write_core(tmp_path, text)
        assert "firmware/firmware.hex" in all_referenced_files(tmp_path)


class TestVendoredFiles:
    def test_collects_vendored_annotated_paths(self, tmp_path: Path):
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              fw:
                files:
                  - tb/test.elf: {file_type: user, tags: [vendored]}
                  - firmware/firmware.hex: {file_type: user}
            targets:
              sim: {flow: sim, filesets: [fw]}
            """
        )
        _write_core(tmp_path, text)
        assert vendored_files(tmp_path) == {"tb/test.elf"}

    def test_no_annotation_yields_empty_set(self, tmp_path: Path):
        _write_core(tmp_path)
        assert vendored_files(tmp_path) == set()

    def test_bare_vendored_key_is_not_honored(self, tmp_path: Path):
        # QA-3: the vendored marker is the CAPI2-valid ``tags: [vendored]``, not
        # a bare ``vendored: true`` (which real fusesoc rejects). The reader must
        # honor only the tag, so a stray bare key marks nothing.
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo:0
            filesets:
              fw:
                files:
                  - tb/test.elf: {file_type: user, vendored: true}
            targets:
              sim: {flow: sim, filesets: [fw]}
            """
        )
        _write_core(tmp_path, text)
        assert vendored_files(tmp_path) == set()


# ---------------------------------------------------------------------------
# [fusesoc] target_cores / target_filter scope knobs — RETIRED (ADR 0030).
# Identity is per-(VLNV, name) and ownership derives from [flows.*]; the scope
# knobs answered nothing that isn't already answered, so they were removed.
# Disambiguation is now tested in TestResolveRef, not here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# selectable_core_closure — scope host-side .core audits to reachable cores
# ---------------------------------------------------------------------------


class TestSelectableCoreClosure:
    """A 208-core monorepo must not fold every unselectable core into the
    host-side .core audits; the closure walks the selectable Targets' CAPI2
    depend graph so only reachable cores are in scope (SETUP-19)."""

    @staticmethod
    def _root(name: str, dep_key: str) -> str:
        # A selectable-Target core whose fileset depends on `dep_key`.
        return textwrap.dedent(
            f"""\
            CAPI=2:
            name: acme:lib:{name}:0
            filesets:
              rtl:
                files:
                  - rtl/{name}.sv: {{file_type: systemVerilogSource}}
                depend: [{dep_key}]
            targets:
              sim:
                flow: sim
                flow_options: {{tool: verilator}}
                filesets: [rtl]
                toplevel: {name}
            """
        )

    @staticmethod
    def _leaf(name: str) -> str:
        # A plain dependency core: no selectable Target, no depends of its own.
        return textwrap.dedent(
            f"""\
            CAPI=2:
            name: acme:lib:{name}:0
            filesets:
              rtl:
                files:
                  - rtl/{name}.sv: {{file_type: systemVerilogSource}}
            targets:
              default:
                filesets: [rtl]
            """
        )

    def test_none_without_seed(self, tmp_path: Path):
        # No seed Targets (ADR 0030) → None → caller audits every core.
        _write_core(tmp_path / "ip")
        assert selectable_core_closure(tmp_path) is None
        assert selectable_core_closure(tmp_path, []) is None

    def test_closure_reaches_transitive_depends(self, tmp_path: Path):
        # top --depend--> dep --depend--> deep; a fourth, unrelated core stays out.
        top = _write_core(tmp_path / "top", self._root("top", "acme:lib:dep"))
        dep = _write_core(
            tmp_path / "dep",
            self._leaf("dep").replace(
                "filesets:\n  rtl:\n    files:",
                "filesets:\n  rtl:\n    depend: [acme:lib:deep]\n    files:",
            ),
        )
        deep = _write_core(tmp_path / "deep", self._leaf("deep"))
        _write_core(tmp_path / "island", self._leaf("island"))  # unreachable

        # Seed the project's declared Target (`sim`, declared only by top).
        closure = selectable_core_closure(tmp_path, ["sim"])
        assert closure == frozenset({top, dep, deep})

    def test_version_independent_depend_match(self, tmp_path: Path):
        # A versioned/operator-qualified depend still reaches the dep core.
        top = _write_core(tmp_path / "top", self._root("top", "(>=acme:lib:dep:1.0)"))
        dep = _write_core(tmp_path / "dep", self._leaf("dep"))
        assert selectable_core_closure(tmp_path, ["sim"]) == frozenset({top, dep})

    def test_unselectable_sibling_target_does_not_widen_closure(self, tmp_path: Path):
        # top's seeded `sim` depends on dep; its *unseeded* `alt` target depends on
        # rogue — rogue must NOT enter the closure (precise per-Target seeding).
        top_text = self._root("top", "acme:lib:dep") + textwrap.dedent(
            """\
              alt:
                flow: sim
                flow_options: {tool: verilator}
                filesets: [alt]
                depend: [acme:lib:rogue]
            """
        )
        # Give `alt` its own fileset so the target is structurally valid.
        top_text = top_text.replace(
            "  rtl:\n",
            "  alt:\n    files:\n      - rtl/alt.sv: {file_type: systemVerilogSource}\n  rtl:\n",
            1,
        )
        top = _write_core(tmp_path / "top", top_text)
        dep = _write_core(tmp_path / "dep", self._leaf("dep"))
        _write_core(tmp_path / "rogue", self._leaf("rogue"))
        # Seed only `sim` (not `alt`), so rogue stays out of the closure.
        closure = selectable_core_closure(tmp_path, ["sim"])
        assert closure == frozenset({top, dep})


# ---------------------------------------------------------------------------
# setup_command — tool_<x> use-flag injection for flow-API Targets
# ---------------------------------------------------------------------------


class TestSetupCommandEdaToolFlag:
    """Flow-API Targets don't set the upstream ``tool_<x>`` use-flag the legacy
    FuseSoC API
    sets, silently dropping `tool_verilator ? (...)` filesets (lowRISC gates
    its C++ harness and lint waivers behind them). setup_command re-injects
    the flag for the declared EDA tool."""

    def test_flow_api_target_gets_upstream_tool_flag(self, tmp_path: Path):
        from booley.fusesoc.fusesoc_registry import setup_command

        _write_core(tmp_path / "ip")  # 'sim': flow: sim, flow_options.eda_tool: verilator
        cmd = setup_command("sim", project_root=tmp_path, build_root=tmp_path / "b")
        joined = " ".join(cmd)
        assert "--flag tool_verilator" in joined
        # run-subcommand option: must come after 'run', before the VLNV.
        assert cmd.index("--flag") > cmd.index("run")
        assert cmd.index("--flag") < cmd.index("::demo_core:0")

    def test_legacy_fusesoc_api_target_gets_no_flag(self, tmp_path: Path):
        from booley.fusesoc.fusesoc_registry import setup_command

        legacy = _CORE_TEXT.replace("    flow: sim\n", "").replace(
            "    flow_options:\n      tool: verilator\n", ""
        )
        _write_core(tmp_path / "ip", legacy)
        cmd = setup_command("sim", project_root=tmp_path, build_root=tmp_path / "b")
        assert "--flag" not in cmd  # legacy API sets tool_verilator natively


class TestHdlSourceSlice:
    """Dependency cores contribute non-HDL EDAM entries (`user` .vmem data,
    copyto'd scripts); rtl_hdl_source_files keeps them out of sv2v/yosys
    (ibex: a dep's check_tool_requirements.py crashed sv2v)."""

    def test_rtl_hdl_source_files_drop_user_files(self, tmp_path: Path):
        edam_text = _EDAM_TEXT + textwrap.dedent(
            """\
            - file_type: user
              name: src/demo_core_0/util/check_tool_requirements.py
              core: '::eda_tool_check:0'
            - file_type: user
              name: src/demo_core_0/sw/firmware.vmem
              core: '::demo_core:0'
            """
        )
        edam = tmp_path / "demo_core_0.eda.yml"
        edam.write_text(edam_text, encoding="utf-8")
        r = parse_edam(edam, target="sim", vlnv="::demo_core:0")
        # Fingerprint-facing slice still sees the data files...
        names = [f.name.split("/")[-1] for f in r.rtl_source_files]
        assert "check_tool_requirements.py" in names
        assert "firmware.vmem" in names
        # ...but the HDL slice fed to sv2v/yosys does not.
        hdl = [f.name.split("/")[-1] for f in r.rtl_hdl_source_files]
        assert hdl == ["counter_pkg.sv", "counter.sv"]


class TestFilesetsAppend:
    """CAPI2 `filesets_append` (the YAML-anchor idiom: `<<: *default_target`
    + filesets_append) must be walked like `filesets` — ibex's booley_sim
    adds its tb-tagged .vmem fileset that way and doctor's tagged-TB audit
    falsely failed it."""

    def test_target_source_files_walks_filesets_append(self, tmp_path: Path):
        from booley.fusesoc.fusesoc_registry import target_source_files

        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo_core:0
            filesets:
              rtl:
                files:
                  - rtl/counter.sv: {file_type: systemVerilogSource}
              fw:
                files:
                  - sw/firmware.vmem: {file_type: user}
                tags: [tb]
            targets:
              default: &default_target
                filesets: [rtl]
              sim:
                <<: *default_target
                flow: sim
                flow_options: {tool: verilator}
                filesets_append: [fw]
                toplevel: counter
            """
        )
        _write_core(tmp_path, text)
        src = target_source_files(tmp_path, "sim")
        assert src.tb_files == ("sw/firmware.vmem",)
        assert src.rtl_source_files == ("rtl/counter.sv",)

    def test_target_source_files_rebases_stealth_core_paths(self, tmp_path: Path):
        """A CAPI2 fileset path is relative to its .core's directory. For a
        state-zone core (ADR 0036 stealth cores) that is NOT the project root,
        so returned paths must be re-based project-relative — consumers join
        them onto the root (doctor's sentinel scan, mutation_tester's DUT list)
        and used to silently read the wrong file, or none at all."""
        from booley.fusesoc.fusesoc_registry import target_source_files

        state_cores = tmp_path / ".booley_project" / "cores"
        state_cores.mkdir(parents=True)
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo-booley:0
            filesets:
              rtl:
                files:
                  - rtl/counter.sv: {file_type: systemVerilogSource}
              tb:
                files:
                  - tb_directed.sv: {file_type: systemVerilogSource}
                tags: [tb]
            targets:
              sim:
                flow: sim
                flow_options: {tool: verilator}
                filesets: [rtl, tb]
                toplevel: tb
            """
        )
        _write_core(state_cores, text)
        src = target_source_files(tmp_path, "sim")
        assert src.rtl_source_files == (".booley_project/cores/rtl/counter.sv",)
        assert src.tb_files == (".booley_project/cores/tb_directed.sv",)

    def test_target_source_files_resolves_every_in_project_symlink(self, tmp_path: Path):
        """RTL and TB resolution links collapse to their tracked repo paths."""
        require_symlinks(tmp_path)
        rtl = tmp_path / "rtl"
        tb = tmp_path / "tb"
        rtl.mkdir()
        tb.mkdir()
        (rtl / "counter.sv").write_text("module counter; endmodule\n")
        (tb / "tb_counter.sv").write_text("module tb_counter; endmodule\n")
        state_cores = tmp_path / ".booley_project" / "cores"
        state_cores.mkdir(parents=True)
        (state_cores / "rtl").symlink_to(rtl, target_is_directory=True)
        (state_cores / "tb").symlink_to(tb, target_is_directory=True)
        _write_core(
            state_cores,
            textwrap.dedent(
                """\
                CAPI=2:
                name: ::demo_core:0
                filesets:
                  rtl: {files: [rtl/counter.sv]}
                  tb:
                    files: [tb/tb_counter.sv]
                    tags: [tb]
                targets:
                  sim: {filesets: [rtl, tb], toplevel: tb_counter}
                """
            ),
        )

        src = target_source_files(tmp_path, "sim")

        assert src.rtl_source_files == ("rtl/counter.sv",)
        assert src.tb_files == ("tb/tb_counter.sv",)

    def test_all_referenced_files_rebases_stealth_core_paths(self, tmp_path: Path):
        """Same re-basing for the every-fileset sweep doctor's tracked-file
        check drives — a stealth core's `fw/boot.hex` is at
        `.booley_project/cores/fw/boot.hex`, not `<root>/fw/boot.hex`."""
        from booley.fusesoc.fusesoc_registry import all_referenced_files

        state_cores = tmp_path / ".booley_project" / "cores"
        state_cores.mkdir(parents=True)
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo-booley:0
            filesets:
              fw:
                files:
                  - fw/boot.hex: {file_type: user, copyto: boot.hex}
            targets:
              sim: {flow: sim, filesets: [fw]}
            """
        )
        _write_core(state_cores, text)
        assert all_referenced_files(tmp_path) == [".booley_project/cores/fw/boot.hex"]

    def test_target_referenced_files_include_conditionals_and_dependencies(
        self, tmp_path: Path
    ) -> None:
        top = textwrap.dedent(
            """\
            CAPI=2:
            name: acme:demo:top:0
            filesets:
              harness:
                files: [tb/top.sv]
                depend: ["flag_fw ? (acme:demo:firmware)"]
              generated:
                files:
                  - generated/boot.hex: {file_type: user}
                  - "flag_extra ? (generated/test.hex)": {file_type: user}
            targets:
              sim:
                flow: sim
                flow_options: {tool: verilator}
                filesets: [harness, "tool_verilator ? (generated)"]
                toplevel: top
            """
        )
        _write_core(tmp_path, top, create_sources=False)
        (tmp_path / "tb").mkdir()
        (tmp_path / "tb" / "top.sv").touch()
        generated = tmp_path / "generated"
        generated.mkdir()
        (generated / "boot.hex").touch()
        (generated / "test.hex").touch()

        dependency = tmp_path / "deps" / "firmware"
        dependency.mkdir(parents=True)
        _write_core(
            dependency,
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:demo:firmware:0
                filesets:
                  images:
                    files:
                      - images/rom.hex: {file_type: user}
                targets:
                  default: {filesets: [images]}
                """
            ),
            create_sources=False,
        )
        (dependency / "images").mkdir()
        (dependency / "images" / "rom.hex").touch()

        assert target_referenced_files(tmp_path, "sim") == (
            "tb/top.sv",
            "generated/boot.hex",
            "generated/test.hex",
            "deps/firmware/images/rom.hex",
        )

    def test_target_referenced_files_exclude_nonconsumed_globs_and_dependency_filesets(
        self, tmp_path: Path
    ) -> None:
        _write_core(
            tmp_path,
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:demo:top:0
                filesets:
                  harness:
                    files: [generated/*.hex]
                    depend: [acme:demo:unused]
                targets:
                  sim: {filesets: [harness]}
                """
            ),
            create_sources=False,
        )
        dependency = tmp_path / "deps" / "unused"
        dependency.mkdir(parents=True)
        _write_core(
            dependency,
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:demo:unused:0
                filesets:
                  data:
                    files: [data/not-consumed.hex]
                """
            ),
            create_sources=False,
        )

        assert target_referenced_files(tmp_path, "sim") == ()

    def test_missing_target_sources_walks_filesets_append(self, tmp_path: Path):
        """The source-existence preflight (`_literal_target_source_paths`) shares
        the blind spot: an appended fileset's missing file must be reported, or
        every built-in's fail-fast preflight silently skips it."""
        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo_core:0
            filesets:
              rtl:
                files:
                  - rtl/counter.sv: {file_type: systemVerilogSource}
              fw:
                files:
                  - sw/firmware.vmem: {file_type: user}
            targets:
              default: &default_target
                filesets: [rtl]
              sim:
                <<: *default_target
                filesets_append: [fw]
                toplevel: counter
            """
        )
        _write_core(tmp_path, text, create_sources=False)  # nothing on disk
        missing = missing_target_sources(tmp_path, "sim")
        assert "sw/firmware.vmem" in missing  # the appended path is walked

    def test_dump_module_detection_walks_filesets_append(self, tmp_path: Path):
        """The trace-overlay readiness check shares the blind spot: a
        booley_vcd_dump.sv fileset added via append would false-warn 'no dump
        module' and provoke a duplicate overlay injection."""
        from booley.fusesoc.fusesoc_trace_overlay import target_includes_dump_module

        text = textwrap.dedent(
            """\
            CAPI=2:
            name: ::demo_core:0
            filesets:
              rtl:
                files:
                  - rtl/counter.sv: {file_type: systemVerilogSource}
              trace:
                files:
                  - dv/booley_vcd_dump.sv: {file_type: systemVerilogSource}
            targets:
              default: &default_target
                filesets: [rtl]
              sim:
                <<: *default_target
                filesets_append: [trace]
                toplevel: counter
            """
        )
        _write_core(tmp_path, text)
        assert target_includes_dump_module(tmp_path, "sim") is True


# ---------------------------------------------------------------------------
# core_target_uses_legacy_fusesoc_api (F-6)
# ---------------------------------------------------------------------------


class TestCoreTargetUsesLegacyFuseSocApi:
    """Legacy Targets declare no Flow, though their EDA tool remains readable."""

    _LEGACY = """\
CAPI=2:
name: ::oc_i2c:0
filesets:
  rtl: {files: [rtl/i2c.v], file_type: verilogSource}
targets:
  sim:
    filesets: [rtl]
    default_tool: icarus
    toplevel: tb
    tools:
      icarus: {iverilog_options: [-g2012]}
  modern:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: icarus}
    toplevel: tb
"""

    def _doc(self, tmp_path: Path):
        _write_core(tmp_path, self._LEGACY)
        return read_core(tmp_path / "design.core")

    def test_upstream_default_tool_target_is_legacy(self, tmp_path: Path):
        assert core_target_uses_legacy_fusesoc_api(self._doc(tmp_path), "sim") is True

    def test_flow_api_target_is_not_legacy(self, tmp_path: Path):
        assert core_target_uses_legacy_fusesoc_api(self._doc(tmp_path), "modern") is False

    def test_missing_target_is_not_legacy(self, tmp_path: Path):
        assert core_target_uses_legacy_fusesoc_api(self._doc(tmp_path), "nope") is False

    def test_legacy_target_still_reports_its_eda_tool(self, tmp_path: Path):
        # The EDA tool is readable (via the upstream default_tool mirror); only flow is absent.
        doc = self._doc(tmp_path)
        assert core_target_eda_tool(doc, "sim") == "icarus"
        assert core_target_flow(doc, "sim") is None

    def test_a_flow_wins_even_with_a_legacy_fusesoc_section(self, tmp_path: Path):
        text = """\
CAPI=2:
name: ::x:0
filesets:
  rtl: {files: [rtl/i2c.v], file_type: verilogSource}
targets:
  hybrid:
    filesets: [rtl]
    flow: sim
    tools: {icarus: {iverilog_options: []}}
"""
        _write_core(tmp_path, text)
        doc = read_core(tmp_path / "design.core")
        assert core_target_uses_legacy_fusesoc_api(doc, "hybrid") is False
