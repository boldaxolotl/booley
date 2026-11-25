"""Tests for fusesoc_registry: .core enumeration + EDAM resolution (ADR 0022)."""

from __future__ import annotations

import functools
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booley.core.host_shell import quote_arg
from booley.flows.synth.recipe import synth_target_tools
from booley.fusesoc import core_projection, fusesoc_registry, selftest_overlay
from booley.fusesoc.core_projection import CoreProjectionError
from booley.fusesoc.fusesoc_registry import (
    DEFAULT_FUSESOC_CMD,
    CoreCollisionError,
    FuseSocError,
    MissingSourceError,
    ResolvedTarget,
    TargetResolutionError,
    all_referenced_files,
    core_schema_errors,
    core_setup_hazards,
    core_target_coverage_errors,
    core_target_doctor_flows,
    core_target_eda_tool,
    core_target_flow,
    core_target_flow_option,
    core_target_is_doctor_selftest,
    core_target_names,
    core_target_uses_legacy_fusesoc_api,
    discover_cores,
    missing_target_sources_for_ref,
    parse_edam,
    possible_target_parameter_names,
    preflight_target_sources_for_ref,
    read_core,
    resolve_target_handle,
    setup_command_for_handle,
    target_fileset_names,
    target_parameter_definitions,
    target_parameter_specs,
    target_source_files_for_ref,
    vendored_files,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    AmbiguousTargetError,
    CoreSources,
    TargetHandle,
    TargetRef,
    UnknownTargetError,
    flow_can_drive,
)
from booley.targets.target_surface import collect_surface
from booley.targets.target_surface import detail_payload as _detail_payload
from booley.targets.target_surface import surface_payload as _surface_payload
from tests.conftest import require_symlinks, symlink_or_skip
from tests.fusesoc_test_support import CORE_TEXT as _CORE_TEXT
from tests.fusesoc_test_support import (
    INHERITED_CORE_TEXT,
    fusesoc_setup_runner,
    relocated_project,
    symlinked_booley_dir,
    touch_declared_sources,
)
from tests.fusesoc_test_support import write_core as _write_core

detail_payload = functools.partial(_detail_payload, synth_tools=synth_target_tools)
surface_payload = functools.partial(_surface_payload, synth_tools=synth_target_tools)

# Spelled out, not imported: the layout is the contract the production constant must match.
STATE_CORES_SUBDIR = "cores"


def _stealth_root(project: Path) -> Path:
    """The default (``.booley``) stealth authored-cores root of *project*."""
    return project / ".booley" / STATE_CORES_SUBDIR


def _handle(project: Path | str, name: str = "sim") -> TargetHandle:
    return TargetCatalog.build(project).select(name)


def _handles(project: Path | str) -> dict[str, TargetHandle]:
    return {handle.name: handle for handle in TargetCatalog.build(project).list()}


def _ref(project: Path, name: str = "sim") -> TargetRef:
    """The declaration a selected handle names, as resolution passes it to the registry."""
    handle = _handle(project, name)
    return TargetRef(handle.name, handle.vlnv, handle.core_file)


def _sources(project: Path, name: str = "sim", **options: bool) -> CoreSources:
    return target_source_files_for_ref(project, _ref(project, name), **options)


def _setup_cmd(project: Path, name: str = "sim") -> list[str]:
    return setup_command_for_handle(_handle(project, name), build_root=project / "b")


def _missing_sources(project: Path, name: str = "sim") -> list[str]:
    return missing_target_sources_for_ref(project, _ref(project, name))


def available_targets(project_root: Path | str) -> list[str]:
    return sorted(_handles(project_root))


def target_eda_tools(project_root: Path | str) -> dict[str, str | None]:
    return {name: handle.eda_tool for name, handle in _handles(project_root).items()}


def doctor_target_selectors(project_root: Path | str, flow_name: str) -> list[str]:
    return [
        handle.selector
        for handle in TargetCatalog.build(project_root).list()
        if flow_name in handle.doctor_flows
    ]


def doctor_target_seed(project_root: Path | str) -> list[str]:
    return list(
        dict.fromkeys(
            handle.selector
            for handle in TargetCatalog.build(project_root).list()
            if handle.doctor_flows
        )
    )


def _fusesoc_cmd() -> list[str]:
    """The real FuseSoC: its console script, else the importable module."""
    if shutil.which("fusesoc"):
        return list(DEFAULT_FUSESOC_CMD)
    return [sys.executable, "-c", "from fusesoc.main import main; main()"]


def _root_core(name: str, dep_key: str) -> str:
    """A selectable-Target core whose fileset depends on *dep_key*."""
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


def _leaf_core(name: str, extra: str = "") -> str:
    """A plain dependency core: no selectable Target; *extra* adds a fileset key line."""
    return textwrap.dedent(
        f"""\
        CAPI=2:
        name: acme:lib:{name}:0
        filesets:
          rtl:
            {extra}
            files:
              - rtl/{name}.sv: {{file_type: systemVerilogSource}}
        targets:
          default:
            filesets: [rtl]
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


def _parsed(tmp_path: Path, text: str, target: str, vlnv: str) -> ResolvedTarget:
    edam = tmp_path / "parsed.eda.yml"
    edam.write_text(text, encoding="utf-8")
    return parse_edam(edam, target=target, vlnv=vlnv)


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

    @pytest.mark.parametrize(
        ("skipped", "marker"),
        [
            (("build/demo_core_0", ".runtime/x"), None),  # generated artifacts
            ((".git/objects/ab",), None),
            # FuseSoC's scanner convention: a FUSESOC_IGNORE file excludes its tree.
            (("vendored", "vendored/nested"), "vendored"),
            # Per-ticket / baseline worktrees under .booley/ carry stale VLNV-colliding
            # copies that would shadow the Project Root's cores and drop its Targets.
            ((".booley/worktrees/scalar_1bfe1733", ".booley/.baseline-wt-123-abc"), None),
        ],
        ids=["build-and-runtime", "git-internals", "fusesoc-ignore", "booley-state"],
    )
    def test_skips_generated_and_ignored_trees(
        self, tmp_path: Path, skipped: tuple[str, ...], marker: str | None
    ):
        _write_core(tmp_path / "ip")
        for directory in skipped:
            _write_core(tmp_path / directory)
        if marker is not None:
            (tmp_path / marker / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        assert [p.parent.name for p in discover_cores(tmp_path)] == ["ip"]

    def test_read_core_parses_capi2_header(self, tmp_path: Path):
        doc = read_core(_write_core(tmp_path))
        assert doc["name"] == "::demo_core:0"
        assert set(doc["targets"]) == {"default", "sim"}
        assert core_target_names(doc) == ["sim"]

    def test_read_core_resolves_inheritance_like_fusesoc(self, tmp_path: Path):
        core = _write_core(tmp_path, INHERITED_CORE_TEXT)  # #5077
        fusesoc_doc = fusesoc_registry.fusesoc_core_parser().read(str(core))
        assert read_core(core)["targets"]["synth_child"] == fusesoc_doc["targets"]["synth_child"]

    @pytest.mark.parametrize(
        "text",
        [
            "- just\n- a\n- list\n",
            "CAPI=2:\na: &a [x]\nb: {<<: *a}\n",
            "CAPI=2:\na: &a {k: s}\nb: {<<: *a, k: {z: 1}}\n",
            "CAPI=2:\ntargets: {sim: &sim {<<: *sim}}\n",
        ],
    )
    def test_read_core_rejects_non_mapping(self, tmp_path: Path, text: str):
        bad = tmp_path / "bad.core"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(FuseSocError):
            read_core(bad)

    def test_read_core_rejects_non_utf8(self, tmp_path: Path):
        bad = tmp_path / "bad.core"
        bad.write_bytes(b"CAPI=2:\nname: ::a:\xff\n")
        with pytest.raises(FuseSocError, match=r"could not read \.core .*bad\.core"):
            read_core(bad)


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
        assert hazards[0].target == tmp_path.resolve()

    @pytest.mark.parametrize(
        ("marker", "link", "target"),
        [
            ("lib", "lib/repo", "."),
            (".", "lib/repo", "."),  # marker at the Project scan root
            (".booley/cores", ".booley/cores/lib/cores", ".booley/cores"),  # at the state root
            (None, "links/source", "source"),  # a link that is not recursive
        ],
        ids=["library-marker", "project-marker", "state-root-marker", "non-recursive"],
    )
    def test_no_hazard_for_ignored_or_non_recursive_link(
        self, tmp_path: Path, marker: str | None, link: str, target: str
    ):
        (tmp_path / target).mkdir(parents=True, exist_ok=True)
        (tmp_path / link).parent.mkdir(parents=True, exist_ok=True)
        if marker is not None:
            (tmp_path / marker / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        symlink_or_skip(tmp_path / link, tmp_path / target, target_is_directory=True)
        assert core_setup_hazards(tmp_path) == []


# ---------------------------------------------------------------------------
# Stealth authored cores: .booley/cores/ as a second root (ADR 0036)
# ---------------------------------------------------------------------------


class TestStealthCores:
    """Stealth projects (Booley usage invisible to the host repo) author their
    cores under .booley/cores/ — the one state-dir subtree scanned as
    a second discovery root and handed to FuseSoC as a second --cores-root."""

    def test_stealth_cores_discovered_worktrees_still_skipped(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(_stealth_root(tmp_path), stealth)
        # Transient state stays invisible even with the stealth root active.
        _write_core(tmp_path / ".booley" / "worktrees" / "t1")
        _write_core(tmp_path / ".booley" / ".baseline-wt-9-x")
        found = discover_cores(tmp_path)
        assert sorted(p.parent.name for p in found) == [STATE_CORES_SUBDIR, "ip"]

    def test_state_dir_marker_does_not_veto_stealth_root(self, tmp_path: Path):
        # Deployed reality: booley init drops FUSESOC_IGNORE at .booley/.
        # The stealth root is scanned as a ROOT, so the ancestor marker (which
        # gates the repo-root walk) must not hide the authored cores.
        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(_stealth_root(tmp_path), stealth)
        (tmp_path / ".booley" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        assert [p.parent.name for p in discover_cores(tmp_path)] == [STATE_CORES_SUBDIR]

    def test_marker_inside_stealth_root_hides_it(self, tmp_path: Path):
        # A FUSESOC_IGNORE *at or under* the stealth root is honored — same
        # semantics FuseSoC's own walk applies to a scan root.
        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(_stealth_root(tmp_path), stealth)
        (_stealth_root(tmp_path) / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        assert discover_cores(tmp_path) == []

    @pytest.mark.parametrize("stealth_vlnv", ["::demo_core:0", "::demo_core:1"])
    def test_cross_root_vlnv_collision_is_fatal(self, tmp_path: Path, stealth_vlnv: str):
        # Same logical VLNV in both roots, whatever the version segment = the
        # worktree-shadowing bug reborn as an authoring mistake; the catalog refuses
        # rather than pick a side.
        _write_core(tmp_path / "ip")
        _write_core(_stealth_root(tmp_path), _CORE_TEXT.replace("::demo_core:0", stealth_vlnv))
        with pytest.raises(CoreCollisionError, match="both core roots"):
            TargetCatalog.build(tmp_path)

    def test_same_zone_version_dedup_stays_legal(self, tmp_path: Path):
        # Two *versions* of one core in the SAME zone keep first-wins semantics.
        stealth_v0 = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        stealth_v1 = _CORE_TEXT.replace("::demo_core:0", "::stealth:1")
        _write_core(_stealth_root(tmp_path) / "a", stealth_v0)
        _write_core(_stealth_root(tmp_path) / "b", stealth_v1)
        assert available_targets(tmp_path) == ["sim"]

    def test_setup_command_adds_second_cores_root(self, tmp_path: Path):
        stealth = _CORE_TEXT.replace("::demo_core:0", "::stealth:0")
        _write_core(_stealth_root(tmp_path), stealth)
        cmd = _setup_cmd(tmp_path)
        roots = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cores-root"]
        assert roots == [str(tmp_path), str(_stealth_root(tmp_path))]
        # Global option: both must precede the 'run' subcommand.
        assert max(i for i, a in enumerate(cmd) if a == "--cores-root") < cmd.index("run")

    def test_setup_command_omits_stealth_root_when_absent(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        cmd = _setup_cmd(tmp_path)
        assert cmd.count("--cores-root") == 1

    def test_explicit_stealth_projects_core_into_repo_root(self, tmp_path: Path):
        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir()
        (booley_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        core_text = (
            _CORE_TEXT.replace("rtl/counter_pkg.sv", "counter_pkg.sv")
            .replace("rtl/counter.sv", "counter.sv")
            .replace("tb/tb_counter.sv", "tb_counter.sv")
        )
        canonical = _write_core(_stealth_root(tmp_path), core_text, create_sources=False)
        for name in ("counter_pkg.sv", "counter.sv", "tb_counter.sv"):
            (tmp_path / name).touch()

        cmd = _setup_cmd(tmp_path)
        projected = tmp_path / ".booley-projected-design.core"

        assert cmd.count("--cores-root") == 1
        assert projected.is_file()
        assert discover_cores(tmp_path) == [canonical]
        sources = _sources(tmp_path)
        assert sources.rtl_source_files == ("counter_pkg.sv", "counter.sv")
        assert sources.tb_files == ("tb_counter.sv",)
        assert _missing_sources(tmp_path) == []

    def test_native_core_ignore_uses_only_private_stealth_registry(self, tmp_path: Path):
        from booley.fusesoc.core_projection import isolated_registry_root

        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir()
        (booley_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        canonical = _write_core(_stealth_root(tmp_path), create_sources=False)
        (tmp_path / "native.core").write_text("CAPI=1\n", encoding="utf-8")

        cmd = _setup_cmd(tmp_path)

        roots = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--cores-root"]
        assert roots == [str(isolated_registry_root(tmp_path))]
        assert discover_cores(tmp_path) == [canonical]

    def test_setup_command_refuses_foreign_expected_projection(self, tmp_path: Path):
        from booley.fusesoc.core_projection import projected_core_path

        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir()
        (booley_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\n",
            encoding="utf-8",
        )
        canonical = _write_core(_stealth_root(tmp_path), create_sources=False)
        projected = projected_core_path(tmp_path, canonical)
        foreign_content = "CAPI=2:\nname: foreign::core:0\n"
        projected.write_text(foreign_content, encoding="utf-8")

        with pytest.raises(TargetResolutionError, match="refusing to overwrite non-Booley file"):
            _setup_cmd(tmp_path)

        assert projected.read_text(encoding="utf-8") == foreign_content


# ---------------------------------------------------------------------------
# Target enumeration through the catalog
# ---------------------------------------------------------------------------


class TestEnumerate:
    def test_maps_name_to_core_and_vlnv(self, tmp_path: Path):
        core = _write_core(tmp_path / "ip")
        handles = _handles(tmp_path)
        assert set(handles) == {"sim"}  # implicit default target is never selectable
        assert handles["sim"].vlnv == "::demo_core:0"
        assert handles["sim"].core_file == core.resolve()
        assert handles["sim"].eda_tool == "verilator"

    def test_core_without_name_is_skipped(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        nameless = _CORE_TEXT.replace("name: ::demo_core:0\n", "")
        _write_core(tmp_path / "nameless", nameless)
        assert available_targets(tmp_path) == ["sim"]

    def test_empty_when_no_core(self, tmp_path: Path):
        """No .core authored → no selectable Targets (the legacy configs.toml
        fallback was removed with decision 23's retirement)."""
        assert available_targets(tmp_path) == []

    def test_doctor_selftest_is_resolvable_but_not_public(self, tmp_path: Path, monkeypatch):
        (tmp_path / "doctor.core").write_text(
            "CAPI=2:\nname: ::doctor:0\ntargets:\n"
            "  lint_selftest_bad:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator, booley: {doctor_selftest: true}}\n",
            encoding="utf-8",
        )

        assert available_targets(tmp_path) == []
        assert target_eda_tools(tmp_path) == {}
        with pytest.raises(UnknownTargetError, match="Unknown target"):
            TargetCatalog.build(tmp_path).select("lint_selftest_bad")

        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        handle = TargetCatalog.build(tmp_path).select("lint_selftest_bad")
        assert handle.selector == "lint_selftest_bad"


# ---------------------------------------------------------------------------
# core_target_eda_tool — declared EDA tool, read from .core YAML
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ({"flow_options": {"tool": "verilator"}}, "verilator"),  # upstream flow_options.tool
        ({"default_tool": "yosys"}, "yosys"),  # upstream default_tool fallback
        ({}, None),
        (None, None),  # target absent
    ],
)
def test_core_target_eda_tool(target: dict[str, object] | None, expected: str | None):
    doc = {"targets": {} if target is None else {"sim": target}}
    assert core_target_eda_tool(doc, "sim") == expected


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
            ("booley: {doctor: [sim, sim]}", "must not contain duplicates"),
            ("booley: {doctor_selftest: bad}", "doctor_selftest must be a boolean"),
            ("booley: {doctor: [sim], mystery: true}", "mystery is not a supported"),
            ("booley: {coverage: whole-run}", "coverage must be a mapping"),
            (
                "booley: {coverage: {reset_included: bad}}",
                "reset_included must be a boolean",
            ),
            (
                "booley: {coverage: {custom_main_hooks: write_hook}}",
                "custom_main_hooks must be an array",
            ),
            (
                "booley: {coverage: {custom_main_hooks: [[start_hook]]}}",
                "custom_main_hooks must contain only strings",
            ),
            (
                "booley: {coverage: {custom_main_hooks: [write_hook, write_hook]}}",
                "must not contain duplicates",
            ),
            (
                "booley: {coverage: {custom_main_hooks: [mystery]}}",
                "contains unknown hook",
            ),
            (
                "booley: {coverage: {mystery: true}}",
                "mystery is not a supported coverage key",
            ),
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

    def test_schema_accepts_coverage_recipe(self, tmp_path: Path):
        core = tmp_path / "coverage.core"
        core.write_text(
            "CAPI=2:\nname: ::coverage:0\ntargets:\n"
            "  sim:\n    flow: sim\n    flow_options:\n"
            "      tool: verilator\n"
            "      booley:\n"
            "        coverage:\n"
            "          reset_included: false\n"
            "          custom_main_hooks: [start_hook, write_hook]\n",
            encoding="utf-8",
        )
        assert core_schema_errors(core) == []

    def test_selected_target_coverage_errors_match_full_schema_message(self, tmp_path: Path):
        core = tmp_path / "coverage.core"
        core.write_text(
            "CAPI=2:\nname: ::coverage:0\ntargets:\n"
            "  sim:\n    flow: sim\n    flow_options:\n"
            "      tool: verilator\n"
            "      booley: {coverage: {reset_included: bad}}\n",
            encoding="utf-8",
        )
        expected = "targets.sim.flow_options.booley.coverage.reset_included must be a boolean"

        assert core_target_coverage_errors(core, "sim") == [expected]
        assert core_schema_errors(core) == [expected]

    def test_selected_target_coverage_accepts_raw_hook_list_and_ignores_sibling(
        self, tmp_path: Path
    ) -> None:
        core = tmp_path / "coverage.core"
        core.write_text(
            "CAPI=2:\nname: ::coverage:0\ntargets:\n"
            "  selected:\n    flow: sim\n    flow_options:\n"
            "      tool: verilator\n"
            "      booley: {coverage: {custom_main_hooks: [start_hook, write_hook]}}\n"
            "  sibling:\n    flow: sim\n    flow_options:\n"
            "      tool: verilator\n"
            "      booley: {coverage: {bogus_key: true}}\n",
            encoding="utf-8",
        )

        assert core_target_coverage_errors(core, "selected") == []
        assert core_target_coverage_errors(core, "sibling") == [
            "targets.sibling.flow_options.booley.coverage.bogus_key "
            "is not a supported coverage key"
        ]

    def test_fpga_doctor_metadata_selects_target(self, tmp_path: Path):
        core = tmp_path / "fpga.core"
        core.write_text(
            "CAPI=2:\nname: ::fpga:0\ntargets:\n"
            "  fpga_board:\n"
            "    flow: generic\n"
            "    flow_options:\n"
            "      tool: verilator\n"
            "      part: xc7a35tcsg324-1\n"
            "      booley: {doctor: [fpga]}\n",
            encoding="utf-8",
        )

        doc = read_core(core)
        assert core_schema_errors(core) == []
        assert core_target_doctor_flows(doc, "fpga_board") == ("fpga",)
        assert doctor_target_selectors(tmp_path, "fpga") == ["fpga_board"]
        assert "fpga_board" in doctor_target_seed(tmp_path)


# ---------------------------------------------------------------------------
# Target selection — bare-unique / vlnv#name / shortest-unambiguous VLNV (ADR 0030)
# ---------------------------------------------------------------------------


def _min_core(vlnv: str, targets: list[str]) -> str:
    """A minimal .core declaring *vlnv* and each name in *targets* (no sources)."""
    body = "".join(
        f"  {t}:\n    flow: lint\n    flow_options: {{tool: verilator}}\n    filesets: [rtl]\n"
        for t in targets
    )
    return f"CAPI=2:\nname: {vlnv}\nfilesets:\n  rtl: {{files: []}}\ntargets:\n" + body


_IBEX = "lowrisc:ibex:ibex_top:0.1"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("sim", _IBEX),  # bare and unique
        ("lowrisc:ibex:ibex_top#lint", _IBEX),  # full VLNV qualifier
        ("ibex:ibex_top#lint", _IBEX),  # shortened library:name qualifier
        ("ibex_top#lint", _IBEX),  # the VLNV name segment alone
        # A bare name two cores declare names every candidate so the user can qualify.
        ("lint", (AmbiguousTargetError, rf"2 cores: {_IBEX}, lowrisc:prim:lfsr:0")),
        ("nope", (UnknownTargetError, "Unknown target")),
        # The name exists but no core matches the qualifier: nothing to disambiguate.
        ("zzz#lint", (UnknownTargetError, "no Target 'lint'")),
    ],
)
def test_selector_resolves_one_declaring_core(
    tmp_path: Path, token: str, expected: str | tuple[type[Exception], str]
):
    # Two cores share 'lint' (ambiguous); only ibex declares 'sim' (unique).
    _write_core(tmp_path / "ibex", _min_core(_IBEX, ["lint", "sim"]), create_sources=False)
    _write_core(
        tmp_path / "prim", _min_core("lowrisc:prim:lfsr:0", ["lint"]), create_sources=False
    )
    if isinstance(expected, str):
        assert _handle(tmp_path, token).vlnv == expected
    else:
        with pytest.raises(expected[0], match=expected[1]):
            _handle(tmp_path, token)


def test_too_short_qualifier_matching_two_cores_is_ambiguous(tmp_path: Path):
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
        _handle(tmp_path, "ibex_top#lint")
    # A longer (library-qualified) form disambiguates.
    assert _handle(tmp_path, "ibex:ibex_top#lint").vlnv == "lowrisc:ibex:ibex_top:0"


def test_same_vlnv_two_versions_collapse_to_first(tmp_path: Path):
    """Two *versions* of one logical core (same vendor:library:name) collapse
    to the first discovered — not a distinct-core ambiguity (ADR 0030)."""
    _write_core(tmp_path / "v0")  # ::demo_core:0 declares 'sim'
    _write_core(tmp_path / "v1", _CORE_TEXT.replace("::demo_core:0", "::demo_core:1"))
    catalog = TargetCatalog.build(tmp_path)
    assert catalog.declaration_count("sim") == 1
    # One logical core → a bare name is NOT ambiguous; it resolves.
    assert catalog.select("sim").vlnv.startswith("::demo_core:")


# ---------------------------------------------------------------------------
# parse_edam — RTL/TB partition by tag (decision 13)
# ---------------------------------------------------------------------------


class TestParseEdam:
    def test_core_fields(self, tmp_path: Path):
        r = _parsed(tmp_path, _EDAM_TEXT, "sim", "::demo_core:0")
        assert r.name == "sim"
        assert r.vlnv == "::demo_core:0"
        assert r.toplevel == "tb_counter"
        assert r.eda_tool == "verilator"
        assert r.configured_eda_tool == "verilator"
        assert r.flow_options == {"tool": "verilator", "flatten": False}
        assert r.build_root == tmp_path
        assert "TESTID" in r.parameters
        assert r.parameters["TESTID"]["paramtype"] == "plusarg"

    @pytest.mark.parametrize(
        ("edam_fields", "declared", "configured"),
        (
            (
                "flow_options: {tool: icarus}\ntool_options: {verilator: {mode: lint}}\n",
                "icarus",
                "icarus",
            ),
            ("flow_options: {}\ntool_options: {icarus: {}}\n", None, "icarus"),
            (
                "flow_options: {}\ntool_options: {icarus: {}, verilator: {}}\n",
                None,
                None,
            ),
            ("flow_options: {tool: ''}\ntool_options: {icarus: {}}\n", "", ""),
            ("flow_options: {}\ntool_options: {}\n", None, None),
        ),
    )
    def test_configured_eda_tool_uses_capi2_api_precedence(
        self,
        tmp_path: Path,
        edam_fields: str,
        declared: str | None,
        configured: str | None,
    ) -> None:
        resolved = _parsed(
            tmp_path,
            f"name: demo\ntoplevel: tb\nfiles: []\nparameters: {{}}\n{edam_fields}",
            "sim",
            "::demo:0",
        )

        assert resolved.eda_tool == declared
        assert resolved.configured_eda_tool == configured

    def test_rtl_tb_partition_by_tag(self, tmp_path: Path):
        r = _parsed(tmp_path, _EDAM_TEXT, "sim", "::demo_core:0")
        assert [f.name.split("/")[-1] for f in r.rtl_files] == [
            "counter_pkg.sv",
            "counter.sv",
        ]
        assert [f.name.split("/")[-1] for f in r.tb_files] == ["tb_counter.sv"]
        assert (
            r.files[0].absolute(r.build_root)
            == (tmp_path / "src/demo_core_0/rtl/counter_pkg.sv").resolve()
        )

    def test_missing_edam_is_resolution_error(self, tmp_path: Path):
        with pytest.raises(TargetResolutionError):
            parse_edam(tmp_path / "nope.eda.yml", target="sim", vlnv="::x:0")

    def test_rtl_hdl_source_files_drop_user_files(self, tmp_path: Path):
        """Dependency cores contribute non-HDL EDAM entries (`user` .vmem data,
        copyto'd scripts); rtl_hdl_source_files keeps them out of sv2v/yosys
        (ibex: a dep's check_tool_requirements.py crashed sv2v)."""
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
        r = _parsed(tmp_path, edam_text, "sim", "::demo_core:0")
        # Fingerprint-facing slice still sees the data files...
        names = [f.name.split("/")[-1] for f in r.rtl_source_files]
        assert "check_tool_requirements.py" in names
        assert "firmware.vmem" in names
        # ...but the HDL slice fed to sv2v/yosys does not.
        hdl = [f.name.split("/")[-1] for f in r.rtl_hdl_source_files]
        assert hdl == ["counter_pkg.sv", "counter.sv"]


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
    def test_includes_stay_out_of_the_compiled_set(self, tmp_path: Path):
        r = _parsed(tmp_path, _EDAM_WITH_INCLUDE, "syn", "::syn_demo:0")
        by_name = {f.name.split("/")[-1]: f for f in r.files}
        assert by_name["defs.svh"].is_include is True
        assert by_name["dut.sv"].is_include is False
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
        r = _parsed(tmp_path, edam_text, "syn", "::syn_demo:0")
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
        r = _parsed(tmp_path, edam_text, "syn", "::syn_demo:0")
        sdc_names = [f.name.split("/")[-1] for f in r.sdc_files]
        assert sdc_names == ["constraints.sdc"]  # tb.sdc excluded
        # An SDC is not (System)Verilog, so it must not reach the HDL source set.
        assert all(not f.name.endswith(".sdc") for f in r.rtl_hdl_source_files)


# ---------------------------------------------------------------------------
# resolve_target_handle — the FuseSoC setup command replaced by the shared runner fake
# ---------------------------------------------------------------------------


def _edam_at(build_root: Path) -> tuple[Path, str]:
    """Where FuseSoC lays out ``<build>/<name>/<target>/<name>.eda.yml``, with its text."""
    return build_root / "demo_core_0" / "sim" / "demo_core_0.eda.yml", _EDAM_TEXT


class TestResolveTargetHandleMocked:
    def test_builds_expected_cli_and_parses_edam(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")
        build_root = tmp_path / "build"
        calls: list[dict[str, object]] = []

        result = resolve_target_handle(
            _handle(project),
            build_root=build_root,
            runner=fusesoc_setup_runner(edam=_edam_at(build_root), calls=calls),
        )
        assert result.toplevel == "tb_counter"
        assert result.eda_tool == "verilator"
        assert result.configured_eda_tool == "verilator"
        # CLI shape: cores-root at project, build-root isolated, --setup, target, vlnv.
        cmd = calls[0]["cmd"]
        assert cmd[: len(DEFAULT_FUSESOC_CMD)] == list(DEFAULT_FUSESOC_CMD)
        assert "--cores-root" in cmd and str(project) in cmd
        assert "--build-root" in cmd and str(build_root) in cmd
        assert "--setup" in cmd
        assert cmd[cmd.index("--target") + 1] == "sim"
        assert cmd[-1] == "::demo_core:0"  # VLNV is the positional core spec
        assert calls[0]["cwd"] == str(project)

    def test_resolution_reads_the_selected_cores_edam(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")
        build_root = tmp_path / "build"
        other = build_root / "aaa_other_0" / "sim" / "aaa_other_0.eda.yml"
        other.parent.mkdir(parents=True)
        other.write_text("name: aaa_other_0\ntoplevel: tb_other\nfiles: []\n", encoding="utf-8")

        result = resolve_target_handle(
            _handle(project),
            build_root=build_root,
            runner=fusesoc_setup_runner(edam=_edam_at(build_root)),
        )

        assert result.toplevel == "tb_counter"
        assert result.edam_path == build_root / "demo_core_0" / "sim" / "demo_core_0.eda.yml"

    def test_resolution_reads_the_tool_api_edam_layout(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")
        build_root = tmp_path / "build"
        edam = build_root / "demo_core_0" / "sim-verilator" / "demo_core_0.eda.yml"

        result = resolve_target_handle(
            _handle(project),
            build_root=build_root,
            runner=fusesoc_setup_runner(edam=(edam, _EDAM_TEXT)),
        )

        assert result.edam_path == edam

    @pytest.mark.parametrize("others", [1, 2])
    def test_resolution_refuses_another_cores_edam_when_ambiguous(
        self, tmp_path: Path, others: int
    ):
        project = tmp_path / "proj"
        _write_core(project / "ip")
        build_root = tmp_path / "build"
        for index in range(others):
            other = build_root / f"other{index}_0" / "sim" / f"other{index}_0.eda.yml"
            other.parent.mkdir(parents=True)
            other.write_text("name: other\ntoplevel: tb_other\nfiles: []\n", encoding="utf-8")

        with pytest.raises(TargetResolutionError) as excinfo:
            resolve_target_handle(
                _handle(project),
                build_root=build_root,
                runner=fusesoc_setup_runner(),
            )

        assert "::demo_core:0" in str(excinfo.value)
        assert f"{others} EDAMs of other resolutions" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [("boom", "(exit 1):\nboom"), ("", "no diagnostic output")],
        ids=["stderr", "silent"],
    )
    def test_setup_failure_reports_diagnostic(self, tmp_path: Path, stderr: str, expected: str):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        with pytest.raises(TargetResolutionError) as excinfo:
            resolve_target_handle(
                _handle(project),
                build_root=tmp_path / "build",
                runner=fusesoc_setup_runner(returncode=1, stderr=stderr),
            )
        assert expected in str(excinfo.value)

    def test_setup_failure_names_unprovided_dependency(self, tmp_path: Path):
        def fail_message(name: str, depend: str) -> str:
            project = tmp_path / name
            _write_core(project / "ip", _root_core("top", depend))
            _write_core(project / "dep", _leaf_core("dep"))
            with pytest.raises(TargetResolutionError) as excinfo:
                resolve_target_handle(
                    _handle(project),
                    build_root=tmp_path / f"build-{name}",
                    runner=fusesoc_setup_runner(returncode=1, stderr="boom"),
                )
            return str(excinfo.value)

        missing = fail_message("missing", "acme:lib:dep, '>=::vlog_tb_utils:1.1'")
        assert missing.endswith(
            "(exit 1):\nboom\nno Project .core provides '>=::vlog_tb_utils:1.1': the Sandbox "
            "has no FuseSoC libraries and cannot fetch them; copy it into the repository as "
            "a .core, or drop the depend: (check first whether the testbench guards its use "
            "with an `ifdef` or defines the module itself)"
        )
        # Provided-but-other-failure: FuseSoC's text stays, no hint is added.
        assert fail_message("provided", "acme:lib:dep").endswith("(exit 1):\nboom")

    def test_missing_fusesoc_binary_raises(self, tmp_path: Path):
        project = tmp_path / "proj"
        _write_core(project / "ip")

        with pytest.raises(TargetResolutionError, match="could not invoke"):
            resolve_target_handle(
                _handle(project),
                build_root=tmp_path / "build",
                runner=fusesoc_setup_runner(error=FileNotFoundError("fusesoc")),
            )

    def test_explicit_tb_tag_is_restored_when_nonstrict_inventory_skips_core(self, tmp_path: Path):
        # FuseSoC 2.4.7's EDAM writer drops the tb tag; the authored one comes back even
        # though a malformed sibling Target makes strict classification refuse the core.
        (tmp_path / "tb.sv").touch()
        (tmp_path / "design.core").write_text(
            "CAPI=2:\nname: ::explicit:0\nfilesets:\n"
            "  tb: {files: [tb.sv], tags: [tb]}\n"
            "targets:\n  sim: {filesets: [tb], toplevel: tb}\n"
            "  malformed: {filesets: [42]}\n",
            encoding="utf-8",
        )
        build_root = tmp_path / "build"
        edam = (
            build_root / "explicit_0" / "sim" / "explicit_0.eda.yml",
            "name: explicit_0\ntoplevel: tb\n"
            "files:\n- {name: tb.sv, file_type: systemVerilogSource}\n",
        )

        resolved = resolve_target_handle(
            _handle(tmp_path), build_root=build_root, runner=fusesoc_setup_runner(edam=edam)
        )

        assert resolved.files[0].is_tb
        with pytest.raises(FuseSocError, match=r"design\.core"):
            fusesoc_registry.classified_sources(tmp_path, strict=True)

    def test_resolving_target_runs_no_extra_core_graph_inspection(self, tmp_path, monkeypatch):
        from booley.fusesoc import target_inspection

        (tmp_path / "plain.core").write_text(
            "CAPI=2:\nname: acme:ip:plain:1.0\nparameters:\n"
            "  W: {datatype: int, paramtype: vlogparam, default: 8}\n"
            "targets:\n  sim:\n    flow: sim\n    flow_options: {tool: verilator}\n"
            "    parameters: [W]\n    toplevel: top\n"
        )
        handle = TargetCatalog.build(tmp_path).select("sim")
        inspected = []
        # private: resolution has no output that shows whether it ran the FuseSoC
        # core-graph inspection; only a spy on the inspector's one graph read can (#1646).
        original = target_inspection._TargetSourceInspector._cores

        def spy(self, **kwargs):
            inspected.append(kwargs["identity"])
            return original(self, **kwargs)

        monkeypatch.setattr(target_inspection._TargetSourceInspector, "_cores", spy)
        build_root = tmp_path / "build"
        edam = (
            build_root / "acme_ip_plain_1.0" / "sim" / "acme_ip_plain_1.0.eda.yml",
            "name: plain\ntoplevel: top\nfiles: []\n",
        )
        resolve_target_handle(
            handle, build_root=build_root, runner=fusesoc_setup_runner(edam=edam)
        )
        assert inspected == []

    def test_fusesoc_setup_sees_the_checkouts_booley_dir(self, tmp_path, monkeypatch):
        """Generators run by ``fusesoc run --setup`` read the checkout's inputs, not Main's."""
        from booley.runtime import booley_dir

        project = tmp_path / "proj"
        _write_core(project / "ip")
        (project / ".booley").mkdir(parents=True)
        main = tmp_path / "main-booley"
        main.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(main))
        booley_dir.reset_cache()
        build_root = tmp_path / "build"

        calls: list[dict[str, object]] = []

        try:
            resolve_target_handle(
                _handle(project),
                build_root=build_root,
                runner=fusesoc_setup_runner(edam=_edam_at(build_root), calls=calls),
            )
        finally:
            booley_dir.reset_cache()

        assert calls[0]["env"]["BOOLEY_DIR"] == str((project / ".booley").resolve())


class TestMissingSourcePreflight:
    """resolve_target_handle fails fast when a Target's fileset paths don't exist."""

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

        calls: list[dict[str, object]] = []

        with pytest.raises(MissingSourceError) as exc_info:
            resolve_target_handle(
                _handle(tmp_path, "sim_base"),
                build_root=tmp_path / "build",
                runner=fusesoc_setup_runner(calls=calls),
            )
        assert calls == []  # FuseSoC is never invoked
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
            f"{quote_arg('worktrees/scalar_1bfe1733')} <commit-ish>"
        ) in msg

    def test_worktree_hint_quotes_the_root(self, tmp_path: Path):
        root = "worktrees/it's base"
        core = self._BASELINE_CORE.replace("worktrees/scalar_1bfe1733", root)
        _write_core(tmp_path, core, create_sources=False)
        with pytest.raises(MissingSourceError) as exc_info:
            resolve_target_handle(
                _handle(tmp_path, "sim_base"),
                build_root=tmp_path / "build",
                runner=fusesoc_setup_runner(),
            )
        assert f"git worktree add {quote_arg(root)} <commit-ish>" in str(exc_info.value)

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
            preflight_target_sources_for_ref(tmp_path, _ref(tmp_path, "sim_plain"))
        msg = str(exc_info.value)
        assert "  - rtl/gone.sv" in msg
        assert "git worktree add" not in msg

    def test_paths_resolve_relative_to_core_dir_not_project_root(self, tmp_path: Path):
        # Core nested under ip/: its rtl/ lives beside the .core, not at root.
        _write_core(tmp_path / "ip")
        preflight_target_sources_for_ref(tmp_path, _ref(tmp_path))  # must not raise
        assert _missing_sources(tmp_path) == []

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
        assert _missing_sources(tmp_path, "sim_glob") == []

    def test_missing_target_sources_walks_filesets_append(self, tmp_path: Path):
        """An appended fileset's missing file must be reported, or every built-in's
        fail-fast preflight silently skips it."""
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
        missing = _missing_sources(tmp_path)
        assert "sw/firmware.vmem" in missing  # the appended path is walked


# ---------------------------------------------------------------------------
# resolve_target_handle — real fusesoc end-to-end (gated on fusesoc being importable)
# ---------------------------------------------------------------------------


class TestResolveTargetHandleReal:
    def test_isolated_constraint_is_absolute_but_file_parameter_remains_authored(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("fusesoc")
        project = tmp_path / "proj"
        cores = project / ".booley" / "cores"
        cores.mkdir(parents=True)
        (project / ".booley" / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        (project / "rtl").mkdir()
        (project / "constraints").mkdir()
        (project / "rtl" / "top.sv").write_text(
            "module top(input logic clk); endmodule\n", encoding="utf-8"
        )
        physical = project / "constraints" / "physical.sdc"
        physical.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
        symlink_or_skip(project / "constraints" / "timing.sdc", physical)
        (cores / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::isolated:0\n"
            "filesets:\n"
            "  rtl:\n"
            "    files: [rtl/top.sv]\n"
            "    file_type: systemVerilogSource\n"
            "  constraints:\n"
            "    files:\n"
            "      - constraints/timing.sdc: {file_type: SDC}\n"
            "parameters:\n"
            "  CONFIG: {datatype: file, paramtype: plusarg, default: config/authored.cfg}\n"
            "targets:\n"
            "  synth:\n"
            "    default_tool: yosys\n"
            "    flow: generic\n"
            "    flow_options: {tool: yosys, arch: xilinx}\n"
            "    filesets: [rtl, constraints]\n"
            "    parameters: [CONFIG]\n"
            "    toplevel: top\n",
            encoding="utf-8",
        )

        resolved = resolve_target_handle(
            _handle(project, "synth"),
            build_root=project / "out",
            fusesoc_cmd=_fusesoc_cmd(),
        )

        assert resolved.sdc_files[0].name == physical.resolve().as_posix()
        assert resolved.parameters["CONFIG"]["default"] == "config/authored.cfg"

    def test_real_setup_resolves_projected_stealth_core(self, tmp_path: Path):
        pytest.importorskip("fusesoc")
        project = tmp_path / "proj"
        (project / ".booley" / "cores").mkdir(parents=True)
        (project / ".booley" / "booley.toml").write_text(
            "[stealth]\nenabled = true\n", encoding="utf-8"
        )
        (project / ".booley" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
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
        _write_core(project / ".booley" / "cores", create_sources=False)

        resolved = resolve_target_handle(
            _handle(project, "sim"),
            build_root=project / "out",
            fusesoc_cmd=_fusesoc_cmd(),
        )

        assert resolved.toplevel == "tb_counter"
        assert (project / ".booley-projected-design.core").is_file()

    def test_real_setup_ignores_invalid_native_core(self, tmp_path: Path):
        pytest.importorskip("fusesoc")
        project = tmp_path / "proj"
        (project / ".booley" / "cores").mkdir(parents=True)
        (project / ".booley" / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        (project / "rtl").mkdir()
        (project / "tb").mkdir()
        (project / "rtl" / "counter_pkg.sv").touch()
        (project / "rtl" / "counter.sv").touch()
        (project / "tb" / "tb_counter.sv").touch()
        _write_core(project / ".booley" / "cores", create_sources=False)
        (project / "broken.core").write_text(
            "CAPI=2:\nname: ::broken:0\nfilesets:\n  bad:\n    files: []\n    depend:\n",
            encoding="utf-8",
        )

        resolved = resolve_target_handle(
            _handle(project, "sim"),
            build_root=project / "out",
            fusesoc_cmd=_fusesoc_cmd(),
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

        result = resolve_target_handle(
            _handle(project, "sim"),
            build_root=tmp_path / "build",
            fusesoc_cmd=_fusesoc_cmd(),
        )
        assert result.toplevel == "tb_counter"
        assert result.eda_tool == "verilator"
        assert [f.name.split("/")[-1] for f in result.tb_files] == ["tb_counter.sv"]
        assert len(result.rtl_files) == 2
        assert (result.build_root / "Makefile").exists()


def test_declared_string_inspection_remains_read_only(tmp_path: Path):
    (tmp_path / "top.sv").write_text('module top #(parameter string NAME="X")(); endmodule\n')
    (tmp_path / "proof.core").write_text("""CAPI=2:
name: proof:string:inspection:1
filesets:
  rtl:
    files: [top.sv]
    file_type: systemVerilogSource
parameters:
  NAME: {datatype: str, paramtype: vlogparam, default: '"MAC"'}
targets:
  synth:
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}
    filesets: [rtl]
    parameters: [NAME]
    toplevel: top
""")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    catalog = TargetCatalog.build(tmp_path)
    inspection = catalog.inspect(catalog.select("synth", for_flow="synth"))
    assert inspection.parameters["NAME"]["datatype"] == "str"
    assert inspection.parameters["NAME"]["default"] == '"MAC"'
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


# ---------------------------------------------------------------------------
# target_source_files_for_ref — pre-resolve RTL/TB partition from the .core (dec 13)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filesets", "target", "rtl", "tb"),
    [
        pytest.param(
            "  rtl:\n    files:\n      - rtl/dut.sv: {file_type: systemVerilogSource}\n",
            "sim: {default_tool: yosys, filesets: [rtl], toplevel: dut}",
            ("rtl/dut.sv",),
            (),
            id="rtl-only",
        ),
        pytest.param(
            "  rtl:\n    files:\n"
            "      - rtl/defs.svh: {file_type: systemVerilogSource, is_include_file: true}\n"
            "      - rtl/dut.sv: {file_type: systemVerilogSource}\n"
            "  tb:\n    files: [tb/tb_dut.sv]\n    tags: [tb]\n",
            "sim: {filesets: [rtl, tb], toplevel: tb_dut}",
            ("rtl/dut.sv",),  # the include header is not a compiled source
            ("tb/tb_dut.sv",),
            id="include-header",
        ),
        pytest.param(
            "  src:\n    files:\n"
            "      - rtl/dut.sv: {file_type: systemVerilogSource}\n"
            "      - tb/tb_dut.sv: {file_type: systemVerilogSource, tags: [tb]}\n",
            "sim: {filesets: [src], toplevel: tb_dut}",
            ("rtl/dut.sv",),
            ("tb/tb_dut.sv",),
            id="per-file-tb-tag",
        ),
        pytest.param(
            "  rtl:\n    files: [rtl/dut.sv]\n  tb:\n    files: [tb/tb_dut.sv]\n",
            "sim: {filesets: [rtl, tb], toplevel: tb_dut}",
            ("rtl/dut.sv", "tb/tb_dut.sv"),  # no tags: [tb] → the testbench reads as RTL
            (),
            id="untagged-tb-fileset",
        ),
        pytest.param(
            # CAPI2 `filesets_append` (the `<<: *default_target` idiom) is walked like
            # `filesets`: ibex's booley_sim adds its tb-tagged .vmem fileset that way.
            "  rtl:\n    files: [rtl/counter.sv]\n"
            "  fw:\n    files: [{sw/firmware.vmem: {file_type: user}}]\n    tags: [tb]\n",
            "default: &default_target {filesets: [rtl]}\n"
            "  sim: {<<: *default_target, flow: sim, flow_options: {tool: verilator},"
            " filesets_append: [fw], toplevel: counter}",
            ("rtl/counter.sv",),
            ("sw/firmware.vmem",),
            id="filesets-append",
        ),
    ],
)
def test_target_sources_partition_by_tb_tag(
    tmp_path: Path, filesets: str, target: str, rtl: tuple[str, ...], tb: tuple[str, ...]
):
    text = f"CAPI=2:\nname: ::demo:0\nfilesets:\n{filesets}targets:\n  {target}\n"
    _write_core(tmp_path, text)
    sources = _sources(tmp_path)
    assert sources.rtl_source_files == rtl
    assert sources.tb_files == tb


def test_target_sources_follow_the_given_declaration(tmp_path: Path):
    """Sources come from the declaration passed in, not a fresh lookup of its bare name."""
    _write_core(tmp_path / "a")
    _write_core(tmp_path / "b", _CORE_TEXT.replace("::demo_core:0", "::other_core:0"))

    sources = _sources(tmp_path, "other_core#sim")

    assert sources.rtl_source_files == ("b/rtl/counter_pkg.sv", "b/rtl/counter.sv")
    assert sources.tb_files == ("b/tb/tb_counter.sv",)


def test_explicit_target_sources_report_only_authored_tb_tags(tmp_path: Path):
    """Doctor's raw tag inventory: an untagged testbench fileset stays RTL, and a
    dependency core contributes its default Target's files."""
    _write_core(
        tmp_path,
        "CAPI=2:\nname: ::untagged:0\nfilesets:\n"
        "  rtl: {files: [rtl/dut.sv], depend: [acme:lib:dep]}\n"
        "  tb: {files: [tb/tb_dut.sv]}\n"
        "  tagged: {files: [tb/tb_tagged.sv], tags: [tb]}\n"
        "targets:\n"
        '  sim: {filesets: [rtl, tb, tagged, "tool_x ? (absent)"], toplevel: tb_dut}\n',
    )
    _write_core(tmp_path / "dep", _leaf_core("dep"))

    sources = fusesoc_registry.explicit_target_sources(tmp_path, _handle(tmp_path))

    assert sources.rtl_source_files == ("dep/rtl/dep.sv", "rtl/dut.sv", "tb/tb_dut.sv")
    assert sources.tb_files == ("tb/tb_tagged.sv",)


class TestTargetSourceFilesDependencyClosure:
    """F-27: the DUT commonly arrives through a dependency, not the root core.

    Ibex's sim Target's root fileset owns the C++/firmware harness while
    `ibex_top_tracing.sv` is contributed transitively, so a root-core-only
    read returned no RTL at all and the Mutation Flow failed before injection.
    """

    @pytest.mark.parametrize(
        ("include_dependencies", "rtl"),
        [(False, ()), (True, ("vendor/core/rtl/dut.sv", "vendor/core/rtl/dut_pkg.sv"))],
        ids=["root-only", "closure"],
    )
    def test_closure_read_finds_the_transitive_dut(
        self, tmp_path: Path, include_dependencies: bool, rtl: tuple[str, ...]
    ):
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
        dep_core = tmp_path / "vendor" / "core" / "dut.core"
        dep_core.parent.mkdir(parents=True)
        dep_core.write_text(dut, encoding="utf-8")
        touch_declared_sources(dep_core)

        src = _sources(tmp_path, include_dependencies=include_dependencies)

        assert src.rtl_source_files == rtl
        # Both inferred C++ harness and explicitly tagged HDL remain TB.
        assert src.tb_files == ("tb/harness.cpp", "tb/tb_top.sv")

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
        src = _sources(tmp_path, include_dependencies=True)
        assert src.rtl_source_files == ("rtl/top.sv",)

    def test_target_source_files_rebases_stealth_core_paths(self, tmp_path: Path):
        """A CAPI2 fileset path is relative to its .core's directory. For a
        state-zone core (ADR 0036 stealth cores) that is NOT the Project Root,
        so returned paths must be re-based project-relative — consumers join
        them onto the root (doctor's sentinel scan, the Mutation Flow's DUT list)
        and used to silently read the wrong file, or none at all."""
        state_cores = tmp_path / ".booley" / "cores"
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
        src = _sources(tmp_path)
        assert src.rtl_source_files == (".booley/cores/rtl/counter.sv",)
        assert src.tb_files == (".booley/cores/tb_directed.sv",)

    def test_target_source_files_resolves_every_in_project_symlink(self, tmp_path: Path):
        """RTL and TB resolution links collapse to their tracked repo paths."""
        require_symlinks(tmp_path)
        rtl = tmp_path / "rtl"
        tb = tmp_path / "tb"
        rtl.mkdir()
        tb.mkdir()
        (rtl / "counter.sv").write_text("module counter; endmodule\n")
        (tb / "tb_counter.sv").write_text("module tb_counter; endmodule\n")
        state_cores = tmp_path / ".booley" / "cores"
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

        src = _sources(tmp_path)

        assert src.rtl_source_files == ("rtl/counter.sv",)
        assert src.tb_files == ("tb/tb_counter.sv",)


# ---------------------------------------------------------------------------
# CAPI2 array-field schema (cheap host-side subset — the "must be array" class
# that Booley's tolerant reader accepts but FuseSoC rejects at resolution).
# ---------------------------------------------------------------------------


class TestCoreSchemaErrors:
    @pytest.mark.parametrize(
        "files",
        [
            "[rtl/foo.sv]",  # a plain path string carries no attrs — nothing to reject
            # Every CAPI2-valid per-file key passes (fusesoc 2.4.6 json_schema.py's
            # ``files`` $def allowlist).
            "[{rtl/foo.sv: {file_type: systemVerilogSource, is_include_file: false,"
            " include_path: rtl, logical_name: work, copyto: build/foo.sv, tags: [synth],"
            " define: {WIDTH: 8}}}]",
        ],
        ids=["bare-string", "every-per-file-key"],
    )
    def test_valid_core_has_no_errors(self, tmp_path: Path, files: str):
        assert core_schema_errors(_write_core(tmp_path)) == []
        text = f"CAPI=2:\nname: ::demo:0\nfilesets:\n  rtl: {{files: {files}}}\n"
        text += "targets:\n  sim: {flow: sim, filesets: [rtl]}\n"
        assert core_schema_errors(_write_core(tmp_path / "valid", text)) == []

    @pytest.mark.parametrize(
        ("fileset", "target", "error"),
        [
            ("{files: [], depend: not_a_list}", "{filesets: [rtl]}", "filesets.rtl.depend"),
            # A bare ``depend:`` parses to None — FuseSoC still rejects it (the exact
            # upstream trap: it passed plain doctor and exploded only at --deep).
            ("{files: [], depend: }", "{filesets: [rtl]}", "filesets.rtl.depend"),
            # A scalar filesets_append splat into per-character fileset names
            # ["r","t","l"] inside target_fileset_names.
            (
                "{files: []}",
                "{filesets: [rtl], filesets_append: rtl}",
                "targets.sim.filesets_append",
            ),
            ("{files: []}", "{filesets: rtl}", "targets.sim.filesets"),
            ("{files: []}", "{parameters_append: WIDTH=8}", "targets.sim.parameters_append"),
        ],
    )
    def test_scalar_array_field_flagged(
        self, tmp_path: Path, fileset: str, target: str, error: str
    ):
        text = f"CAPI=2:\nname: ::demo:0\nfilesets:\n  rtl: {fileset}\n"
        text += f"targets:\n  sim: {target}\n"
        assert f"{error} must be array" in core_schema_errors(_write_core(tmp_path, text))

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

    @pytest.mark.parametrize(
        ("reader", "target_def"),
        [
            (target_fileset_names, {"filesets": ["rtl"]}),
            (target_parameter_specs, {"parameters": ["WIDTH=8"]}),
        ],
        ids=["filesets", "parameters"],
    )
    def test_every_target_key_a_reader_splats_is_schema_audited(
        self, tmp_path: Path, reader, target_def: dict[str, object]
    ):
        """Drift guard: the pre-resolve readers and the schema's array-field table are
        kept by hand in two places; ``filesets_append`` once existed only on the reader
        side, so a malformed scalar passed the audit and splat into per-character
        names. The consumed keys come from the reader itself."""
        consumed: set[str] = set()

        class _Recorder(dict):
            def get(self, key, default=None):
                consumed.add(key)
                return super().get(key, default)

        # Non-empty so the reader's `target_def or {}` guard keeps the recorder.
        reader(_Recorder(target_def))

        assert consumed, "reader consumed no keys — recorder wiring broken"
        for key in sorted(consumed):
            core = tmp_path / f"{key}.core"
            core.write_text(
                f"CAPI=2:\nname: ::demo:0\ntargets:\n  sim: {{{key}: scalar}}\n",
                encoding="utf-8",
            )
            assert f"targets.sim.{key} must be array" in core_schema_errors(core)


def test_possible_target_parameters_preserve_values_and_conditionals() -> None:
    target = {
        "parameters": ["MESSAGE=hello world"],
        "parameters_append": [
            "tool_icarus ? (ENABLE_ZBB=1)",
            "!tool_icarus ? (ENABLE_ZBB=0)",
        ],
    }

    assert possible_target_parameter_names(target) == ["MESSAGE", "ENABLE_ZBB"]


def test_target_parameter_definitions_return_only_local_declarations() -> None:
    document = {
        "parameters": {"WIDTH": {"datatype": "int", "paramtype": "vlogparam"}},
    }
    target = {"parameters": ["WIDTH=8", "DEPENDENCY_MODE=fast"]}

    assert target_parameter_definitions(document, target) == {
        "WIDTH": {"datatype": "int", "paramtype": "vlogparam"}
    }


def test_core_target_flow_option() -> None:
    doc = {"targets": {"synth": {"flow": "generic", "flow_options": {"arch": "xilinx"}}}}
    assert core_target_flow_option(doc, "synth", "arch") == "xilinx"
    assert core_target_flow_option(doc, "synth", "missing") is None
    assert core_target_flow_option(doc, "nope", "arch") is None


# A stealth core's paths are re-based onto the Project Root, as doctor's tracked-file
# check joins them there: its `fw/boot.hex` is `.booley/cores/fw/boot.hex`.
@pytest.mark.parametrize("location", ["", ".booley/cores/"], ids=["project-root", "stealth"])
def test_all_referenced_files_lists_every_fileset_file_including_data(
    tmp_path: Path, location: str
):
    # A file_type: user data file (e.g. firmware hex) must be surfaced so the
    # untracked-file doctor check can see it.
    text = _CORE_TEXT.replace(
        "      - tb/tb_counter.sv: {file_type: systemVerilogSource}\n",
        "      - tb/tb_counter.sv: {file_type: systemVerilogSource}\n"
        "      - firmware/firmware.hex: {file_type: user}\n",
    )
    _write_core(tmp_path / location, text)
    assert sorted(all_referenced_files(tmp_path)) == [
        f"{location}firmware/firmware.hex",
        f"{location}rtl/counter.sv",
        f"{location}rtl/counter_pkg.sv",
        f"{location}tb/tb_counter.sv",
    ]


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ("{file_type: user, tags: [vendored]}", {"tb/test.elf"}),
        ("{file_type: user}", set()),
        # QA-3: the marker is the CAPI2-valid ``tags: [vendored]``; a bare
        # ``vendored: true`` (which real fusesoc rejects) marks nothing.
        ("{file_type: user, vendored: true}", set()),
    ],
    ids=["tag", "no-annotation", "bare-key"],
)
def test_vendored_files(tmp_path: Path, attrs: str, expected: set[str]):
    text = textwrap.dedent(
        f"""\
        CAPI=2:
        name: ::demo:0
        filesets:
          fw:
            files:
              - tb/test.elf: {attrs}
              - firmware/firmware.hex: {{file_type: user}}
        targets:
          sim: {{flow: sim, filesets: [fw]}}
        """
    )
    _write_core(tmp_path, text)
    assert vendored_files(tmp_path) == expected


# ---------------------------------------------------------------------------
# core_closure — scope host-side .core audits to reachable cores
# ---------------------------------------------------------------------------


class TestSelectableCoreClosure:
    """A 208-core monorepo must not fold every unselectable core into the
    host-side .core audits; the closure walks the selectable Targets' CAPI2
    depend graph so only reachable cores are in scope (SETUP-19)."""

    def test_none_without_seed(self, tmp_path: Path):
        # No seed Targets (ADR 0030) → None → caller audits every core.
        _write_core(tmp_path / "ip")
        catalog = TargetCatalog.build(tmp_path)
        assert catalog.core_closure([]) is None

    def test_closure_reaches_transitive_depends(self, tmp_path: Path):
        # top --depend--> dep --depend--> deep; a fourth, unrelated core stays out.
        top = _write_core(tmp_path / "top", _root_core("top", "acme:lib:dep"))
        dep = _write_core(tmp_path / "dep", _leaf_core("dep", "depend: [acme:lib:deep]"))
        # A dependency core needs no targets: the walk reads its filesets alone.
        deep = _write_core(tmp_path / "deep", "CAPI=2:\nname: acme:lib:deep:0\nfilesets: {}\n")
        _write_core(tmp_path / "island", _leaf_core("island"))  # unreachable

        # Seed the project's declared Target (`sim`, declared only by top).
        catalog = TargetCatalog.build(tmp_path)
        closure = catalog.core_closure([catalog.select("sim")])
        assert closure == frozenset({top, dep, deep})

    def test_version_independent_depend_match(self, tmp_path: Path):
        # A versioned/operator-qualified depend still reaches the dep core.
        top = _write_core(tmp_path / "top", _root_core("top", "(>=acme:lib:dep:1.0)"))
        dep = _write_core(tmp_path / "dep", _leaf_core("dep"))
        catalog = TargetCatalog.build(tmp_path)
        assert catalog.core_closure([catalog.select("sim")]) == frozenset({top, dep})

    def test_unprovided_dependencies_match_by_identity(self, tmp_path: Path):
        # A versioned, operator-prefixed depend on a Project core is provided by
        # identity; an unprovided one is named as written, every spelling of it.
        _write_core(
            tmp_path / "top",
            _root_core("top", '(>=acme:lib:dep:1.0), "tool_verilator? (>=::vlog_tb_utils:1.1)"'),
        )
        _write_core(tmp_path / "dep", _leaf_core("dep", "depend: ['::vlog_tb_utils:2.0']"))
        catalog = TargetCatalog.build(tmp_path)
        assert catalog.unprovided_dependencies(catalog.select("sim")) == (
            "::vlog_tb_utils:2.0",
            ">=::vlog_tb_utils:1.1",
        )

    def test_unprovided_dependencies_include_depend_append(self, tmp_path: Path):
        # FuseSoC merges a fileset's depend_append into depend: both are collected,
        # directly and through a Project core.
        _write_core(
            tmp_path / "top",
            _root_core("top", "acme:lib:dep").replace(
                "    depend:", "    depend_append: ['::appended:0']\n    depend:"
            ),
        )
        _write_core(tmp_path / "dep", _leaf_core("dep", "depend_append: ['::deep_appended:0']"))
        catalog = TargetCatalog.build(tmp_path)
        assert catalog.unprovided_dependencies(catalog.select("sim")) == (
            "::appended:0",
            "::deep_appended:0",
        )

    def test_unprovided_dependency_hint_names_each_of_several(self):
        hint = fusesoc_registry.unprovided_dependency_hint(["::a:0", "::b:0"])
        assert hint.startswith("no Project .core provides '::a:0', '::b:0': ")
        assert "copy each into the repository as a .core, or drop its depend:" in hint

    def test_unselectable_sibling_target_does_not_widen_closure(self, tmp_path: Path):
        # top's seeded `sim` depends on dep; its *unseeded* `alt` target depends on
        # rogue — rogue must NOT enter the closure (precise per-Target seeding).
        top_text = _root_core("top", "acme:lib:dep") + textwrap.dedent(
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
        dep = _write_core(tmp_path / "dep", _leaf_core("dep"))
        _write_core(tmp_path / "rogue", _leaf_core("rogue"))
        # Seed only `sim` (not `alt`), so rogue stays out of the closure.
        catalog = TargetCatalog.build(tmp_path)
        closure = catalog.core_closure([catalog.select("sim")])
        assert closure == frozenset({top, dep})


# ---------------------------------------------------------------------------
# setup_command_for_handle — tool_<x> use-flag injection for flow-API Targets
# ---------------------------------------------------------------------------


class TestSetupCommandEdaToolFlag:
    """Flow-API Targets don't set the upstream ``tool_<x>`` use-flag the legacy
    FuseSoC API
    sets, silently dropping `tool_verilator ? (...)` filesets (lowRISC gates
    its C++ harness and lint waivers behind them). setup_command_for_handle re-injects
    the flag for the declared EDA tool."""

    def test_flow_api_target_gets_upstream_tool_flag(self, tmp_path: Path):
        _write_core(tmp_path / "ip")  # 'sim': flow: sim, flow_options.eda_tool: verilator
        cmd = _setup_cmd(tmp_path)
        joined = " ".join(cmd)
        assert "--flag tool_verilator" in joined
        # run-subcommand option: must come after 'run', before the VLNV.
        assert cmd.index("--flag") > cmd.index("run")
        assert cmd.index("--flag") < cmd.index("::demo_core:0")

    def test_legacy_fusesoc_api_target_gets_no_flag(self, tmp_path: Path):
        legacy = _CORE_TEXT.replace("    flow: sim\n", "").replace(
            "    flow_options:\n      tool: verilator\n", ""
        )
        _write_core(tmp_path / "ip", legacy)
        cmd = _setup_cmd(tmp_path)
        assert "--flag" not in cmd  # legacy API sets tool_verilator natively

    def test_handle_setup_transports_backend_options_after_vlnv(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

        default = setup_command_for_handle(handle, build_root=tmp_path / "b")
        explicit_empty = setup_command_for_handle(
            handle, build_root=tmp_path / "b", backend_arguments=()
        )
        injected = setup_command_for_handle(
            handle,
            build_root=tmp_path / "b",
            backend_arguments=("-j4", "VM_PARALLEL_BUILDS=1"),
        )

        assert default == explicit_empty
        assert injected[:-1] == default
        assert injected[-1] == "--make_options=-j4 VM_PARALLEL_BUILDS=1"


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
  hybrid:
    filesets: [rtl]
    flow: sim
    tools: {icarus: {iverilog_options: []}}
"""

    # A flow wins even with a legacy FuseSoC ``tools`` section (hybrid).
    @pytest.mark.parametrize(
        ("target", "legacy"),
        [("sim", True), ("modern", False), ("hybrid", False), ("nope", False)],
    )
    def test_only_a_flowless_target_is_legacy(self, tmp_path: Path, target: str, legacy: bool):
        doc = read_core(_write_core(tmp_path, self._LEGACY))
        assert core_target_uses_legacy_fusesoc_api(doc, target) is legacy

    def test_legacy_target_still_reports_its_eda_tool(self, tmp_path: Path):
        # The EDA tool is readable (via the upstream default_tool mirror); only flow is absent.
        doc = read_core(_write_core(tmp_path, self._LEGACY))
        assert core_target_eda_tool(doc, "sim") == "icarus"
        assert core_target_flow(doc, "sim") is None


@pytest.mark.parametrize("eda_tool", ["verilator", "verible", "veriblelint"])
@pytest.mark.parametrize("options", [None, {}, {"ruleset": "all"}, {"tool": ""}, {"tool": None}])
def test_mixed_lint_enumeration_provenance(tmp_path, eda_tool, options):
    import yaml

    target = {"flow": "lint", "default_tool": eda_tool}
    if options is not None:
        target["flow_options"] = options
    (tmp_path / "mixed.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump({"name": "acme:ip:mixed:1.0", "targets": {"lint_mixed": target}})
    )
    handle = _handles(tmp_path)["lint_mixed"]
    assert handle.eda_tool == eda_tool
    assert handle.lint_flow_eda_tool_missing
    command = _setup_cmd(tmp_path, "lint_mixed")
    assert f"tool_{eda_tool}" in command


@pytest.mark.parametrize(
    "flow,eda_tool",
    [
        (None, "verible"),
        (None, "veriblelint"),
        ("sim", "verilator"),
        ("generic", "yosys"),
        ("lint", "yosys"),
        ("lint", "vivado"),
        ("lint", "slang"),
    ],
)
def test_mixed_lint_authored_selection_controls(tmp_path, flow, eda_tool):
    import yaml

    target = {"default_tool": eda_tool}
    if flow is not None:
        target["flow"] = flow
    (tmp_path / "control.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump({"name": "acme:ip:control:1.0", "targets": {"control": target}})
    )
    handle = _handles(tmp_path)["control"]
    assert handle.eda_tool == eda_tool
    assert handle.lint_flow_eda_tool_missing is (flow == "lint")


@pytest.mark.parametrize("eda_tool", ["verilator", "verible", "veriblelint", "slang"])
def test_explicit_lint_option_precedence_control(tmp_path, eda_tool):
    (tmp_path / "control.core").write_text(
        "CAPI=2:\nname: acme:ip:control:1.0\ntargets:\n  lint_control:\n    flow: lint\n    default_tool: yosys\n"
        + f"    flow_options: {{tool: {eda_tool}}}\n"
    )
    handle = _handles(tmp_path)["lint_control"]
    assert handle.eda_tool == eda_tool
    assert handle.lint_flow_eda_tool_missing is False
    assert flow_can_drive("lint", handle) is (eda_tool != "slang")


# ---------------------------------------------------------------------------
# #2157: a symlinked Booley Directory entry is the same stealth core root
# ---------------------------------------------------------------------------

_SYMLINK_MODES = {
    "projected": "[stealth]\nenabled = true\n",
    "isolated": "[stealth]\nenabled = true\nignore_native_cores = true\n",
}


def _stealth_layout(tmp_path: Path, mode: str, *, symlinked: bool) -> Path:
    """One Project whose `.booley` is a real directory or a directory symlink."""
    project = tmp_path / ("linked" if symlinked else "real")
    project.mkdir()
    if symlinked:
        booley_dir = symlinked_booley_dir(project, tmp_path / "external-booley")
    else:
        booley_dir = project / ".booley"
        (booley_dir / "cores").mkdir(parents=True)
    (booley_dir / "booley.toml").write_text(_SYMLINK_MODES[mode], encoding="utf-8")
    (booley_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    _write_core(booley_dir / "cores", _CORE_TEXT, create_sources=False)
    for relative in ("rtl/counter_pkg.sv", "rtl/counter.sv", "tb/tb_counter.sv"):
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_text("// fixture\n", encoding="utf-8")
    return project


class TestSymlinkedBooleyDirectory:
    @pytest.mark.parametrize("mode", sorted(_SYMLINK_MODES))
    def test_core_library_plan_matches_real_layout(self, tmp_path: Path, mode: str):
        def operational_view(project: Path) -> list[tuple[str, str]]:
            plan = fusesoc_registry.prepare_core_library_plan(project)
            view = []
            for core in discover_cores(project):
                operational = plan.operational_core(core)
                marker = operational.read_text(encoding="utf-8").splitlines()[1]
                assert marker.startswith("# Booley stealth core projection: ")
                view.append((operational.name, marker))
            return view

        real = operational_view(_stealth_layout(tmp_path, mode, symlinked=False))
        linked = operational_view(_stealth_layout(tmp_path, mode, symlinked=True))

        assert linked == real
        assert real and real[0][1].endswith(".booley/cores/design.core")

    @pytest.mark.parametrize("mode", sorted(_SYMLINK_MODES))
    def test_targets_detail_reaches_fusesoc(self, tmp_path: Path, mode: str):
        pytest.importorskip("fusesoc")
        project = _stealth_layout(tmp_path, mode, symlinked=True)
        listing = surface_payload(collect_surface(project), project)
        payload = detail_payload(
            project,
            "sim",
            fusesoc_cmd=[sys.executable, "-c", "from fusesoc.main import main; main()"],
        )

        assert payload.get("resolved_error") is None
        assert payload["resolved"]["toplevel"] == "tb_counter"
        assert payload["core_file"] == ".booley/cores/design.core"
        assert [core["core_file"] for core in listing["cores"]] == [".booley/cores/design.core"]

    @pytest.mark.parametrize("mode", sorted(_SYMLINK_MODES))
    def test_source_below_entry_classifies_project_relative(self, tmp_path: Path, mode: str):
        """A source declared under a linked `.booley` keeps its `.booley/...` spelling.

        Strict classification (reviewer, Specialist workspace) refused it as
        escaping the Project Root because the entry link was resolved away.
        """

        def tb_files(project: Path) -> tuple[str, ...]:
            booley_dir = project / ".booley"
            (booley_dir / "sim").mkdir()
            (booley_dir / "sim" / "tb_extra.sv").write_text("module x; endmodule\n", "utf-8")
            core = booley_dir / "cores" / "design.core"
            declared = "- tb/tb_counter.sv: {file_type: systemVerilogSource}\n"
            text = core.read_text(encoding="utf-8")
            assert declared in text
            extra = declared.replace("tb/tb_counter.sv", ".booley/sim/tb_extra.sv")
            indent = text[: text.index(declared)].rsplit("\n", 1)[1]
            core.write_text(text.replace(declared, declared + indent + extra), "utf-8")
            return fusesoc_registry.classified_sources(project, strict=True).tb_files

        real = tb_files(_stealth_layout(tmp_path, mode, symlinked=False))
        linked = tb_files(_stealth_layout(tmp_path, mode, symlinked=True))

        assert ".booley/sim/tb_extra.sv" in real
        assert linked == real

    def test_canonical_path_follows_only_the_entry_link(self, tmp_path: Path):
        """Reaching the entry's target by any other route stays outside the Project."""
        project = tmp_path / "project"
        (project / "rtl").mkdir(parents=True)
        external = symlinked_booley_dir(project, tmp_path / "external-booley")
        (external / "sim").mkdir()
        (external / "sim" / "a.cc").write_text("", encoding="utf-8")
        symlink_or_skip(project / "rtl" / "sneaky.cc", external / "sim" / "a.cc")
        physical = (external / "sim" / "a.cc").resolve().as_posix()

        assert (
            fusesoc_registry.canonical_project_path(project, ".booley/sim/a.cc")
            == ".booley/sim/a.cc"
        )
        assert fusesoc_registry.canonical_project_path(project, "rtl/sneaky.cc") == physical
        assert (
            fusesoc_registry.canonical_project_path(project, "../external-booley/sim/a.cc")
            == physical
        )
        assert (
            fusesoc_registry.canonical_project_path(project, external / "sim" / "a.cc") == physical
        )

    @pytest.mark.parametrize("mode", sorted(_SYMLINK_MODES))
    def test_link_below_entry_pointing_outside_still_escapes(self, tmp_path: Path, mode: str):
        """Strict classification refuses a link below the entry that leaves the Project."""
        project = _stealth_layout(tmp_path, mode, symlinked=True)
        outside = tmp_path / "outside.sv"
        outside.write_text("module o; endmodule\n", encoding="utf-8")
        (project / ".booley" / "sim").mkdir()
        symlink_or_skip(project / ".booley" / "sim" / "esc.sv", outside)
        core = project / ".booley" / "cores" / "design.core"
        declared = "- tb/tb_counter.sv: {file_type: systemVerilogSource}\n"
        text = core.read_text(encoding="utf-8")
        indent = text[: text.index(declared)].rsplit("\n", 1)[1]
        extra = declared.replace("tb/tb_counter.sv", ".booley/sim/esc.sv")
        core.write_text(text.replace(declared, declared + indent + extra), "utf-8")

        with pytest.raises(FuseSocError, match=r"escapes Project Root: \.booley/sim/esc\.sv"):
            fusesoc_registry.classified_sources(project, strict=True)

    @pytest.mark.parametrize("mode", sorted(_SYMLINK_MODES))
    def test_link_inside_core_root_pointing_outside_keeps_today_outcome(
        self, tmp_path: Path, mode: str
    ):
        """A `.core` link leaving the core root is not adopted (AC6)."""
        project = tmp_path / "project"
        project.mkdir()
        external = symlinked_booley_dir(project, tmp_path / "external-booley")
        (external / "booley.toml").write_text(_SYMLINK_MODES[mode], encoding="utf-8")
        (external / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        foreign = tmp_path / "outside" / "foreign.core"
        foreign.parent.mkdir()
        foreign.write_text("CAPI=2:\nname: ::foreign:0\n", encoding="utf-8")
        symlink_or_skip(external / "cores" / "foreign.core", foreign)

        if mode == "isolated":
            with pytest.raises(
                CoreProjectionError, match=r"outside the stealth core root.*foreign\.core"
            ):
                fusesoc_registry.prepare_core_library_plan(project)
            return
        plan = fusesoc_registry.prepare_core_library_plan(project)
        # The outside core stays its own operational core, not a projection.
        assert plan.operational_core(foreign).samefile(foreign)


_VERDICT_CORE = """\
CAPI=2:
name: acme:ip:verdict:0
filesets:
  rtl: {{files: [{path}: {{file_type: systemVerilogSource}}]}}
targets:
  lint: {{flow: lint, filesets: [rtl], toplevel: top{extra}}}
"""


def test_fusesoc_core_rejection_matches_fusesoc_on_valid_and_invalid_cores(tmp_path: Path) -> None:
    """Valid cores (incl. Windows separators inspection normalizes) pass; unknown keys fail."""
    valid = tmp_path / "valid.core"
    valid.write_text(_VERDICT_CORE.format(path="rtl\\top.sv", extra=""), encoding="utf-8")
    assert fusesoc_registry.fusesoc_core_rejection(valid) is None

    invalid = tmp_path / "invalid.core"
    invalid.write_text(
        _VERDICT_CORE.format(path="rtl/top.sv", extra=", bogus: 1"), encoding="utf-8"
    )
    rejection = fusesoc_registry.fusesoc_core_rejection(invalid)
    assert rejection is not None
    assert rejection.target == "lint"
    assert "bogus" in rejection.message
    assert not rejection.misplaced_booley


def test_fusesoc_core_rejection_ignores_files_fusesoc_skips_silently(tmp_path: Path) -> None:
    """FuseSoC's scan skips a core without the ``CAPI=2:`` first line: no verdict."""
    headerless = tmp_path / "headerless.core"
    headerless.write_text("name: acme:ip:x:0\ntargets: {a: {bogus: 1}}\n", encoding="utf-8")
    assert fusesoc_registry.fusesoc_core_rejection(headerless) is None


# ---------------------------------------------------------------------------
# #2893: build_library_plan gives isolated builds a fresh private mirror
# ---------------------------------------------------------------------------


def _library_plan_project(tmp_path: Path, stealth: str | None) -> Path:
    root = tmp_path / "proj"
    (root / ".booley" / "cores").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(stealth or "", encoding="utf-8")
    return root


@pytest.mark.parametrize("stealth", [None, "[stealth]\nenabled = true\n"])
def test_build_library_plan_is_none_without_isolation(tmp_path: Path, stealth: str | None) -> None:
    root = _library_plan_project(tmp_path, stealth)
    build = tmp_path / "build"
    (build / "old-tree").mkdir(parents=True)
    assert fusesoc_registry.build_library_plan(root, build) is None
    assert (build / "old-tree").is_dir()
    assert not (build / "core-mirror").exists()


def test_build_library_plan_clears_build_root_directories(tmp_path: Path) -> None:
    root = _library_plan_project(
        tmp_path, "[stealth]\nenabled = true\nignore_native_cores = true\n"
    )
    build = tmp_path / "build"
    (build / "design_0" / "lint").mkdir(parents=True)
    (build / "core-mirror").mkdir()
    (build / "run.log").write_text("keep\n", encoding="utf-8")
    plan = fusesoc_registry.build_library_plan(root, build)
    assert plan is not None
    assert plan.roots == ((build / "core-mirror").resolve(),)
    assert not (build / "design_0").exists()
    assert (build / "run.log").read_text(encoding="utf-8") == "keep\n"


def test_build_library_plan_accepts_missing_build_root(tmp_path: Path) -> None:
    root = _library_plan_project(
        tmp_path, "[stealth]\nenabled = true\nignore_native_cores = true\n"
    )
    build = tmp_path / "not" / "yet"
    plan = fusesoc_registry.build_library_plan(root, build)
    assert plan is not None
    assert (build / "core-mirror").is_dir()


def test_build_library_plan_reports_unclearable_build_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _library_plan_project(
        tmp_path, "[stealth]\nenabled = true\nignore_native_cores = true\n"
    )
    build = tmp_path / "build"
    (build / "old-tree").mkdir(parents=True)

    def _refuse(_path: object) -> None:
        raise OSError("busy")

    monkeypatch.setattr(fusesoc_registry.shutil, "rmtree", _refuse)
    with pytest.raises(fusesoc_registry.TargetResolutionError, match="could not mirror"):
        fusesoc_registry.build_library_plan(root, build)


# ---------------------------------------------------------------------------
# #2917: core discovery follows a relocated Booley Directory
# ---------------------------------------------------------------------------


def _relocated_stealth_project(tmp_path: Path, mode: str = "") -> tuple[Path, Path]:
    """A Project whose Booley Directory is ``state`` and holds one stealth core.

    Deliberately no ``state/FUSESOC_IGNORE``: the directory itself, not the
    marker, must keep worktree copies out of discovery.
    """
    project = tmp_path / "proj"
    state = relocated_project(project)
    (state / "booley.toml").write_text(mode, encoding="utf-8")
    _write_core(state / "cores", _CORE_TEXT.replace("::demo_core:0", "::stealth:0"))
    for relative in ("rtl/counter_pkg.sv", "rtl/counter.sv", "tb/tb_counter.sv"):
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_text("// fixture\n", encoding="utf-8")
    return project, state


class TestRelocatedBooleyDirectory:
    def test_discovery_scans_stealth_root_and_skips_the_directory(self, tmp_path: Path):
        project, state = _relocated_stealth_project(tmp_path)
        _write_core(state / "worktrees" / "w" / "ip", _CORE_TEXT.replace("::demo_core:0", "::s:0"))
        assert discover_cores(project) == [state / "cores" / "design.core"]

    def test_cross_root_collision_names_the_relocated_zone(self, tmp_path: Path):
        project, _state = _relocated_stealth_project(tmp_path)
        _write_core(project / "ip", _CORE_TEXT.replace("::demo_core:0", "::stealth:0"))
        with pytest.raises(CoreCollisionError, match=r"both core roots.*\(state/cores/\)"):
            TargetCatalog.build(project)

    def test_snapshot_id_tracks_relocated_config(self, tmp_path: Path):
        project, state = _relocated_stealth_project(tmp_path)
        before = fusesoc_registry.target_snapshot_id(project)
        (state / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        assert fusesoc_registry.target_snapshot_id(project) != before

    def test_library_ignore_and_hazard_scan_skip_the_directory(self, tmp_path: Path):
        project, state = _relocated_stealth_project(tmp_path)
        worktree = state / "worktrees" / "w"
        worktree.mkdir(parents=True)
        symlink_or_skip(worktree / "self", worktree, target_is_directory=True)
        plan = fusesoc_registry.prepare_core_library_plan(project)
        assert str(state.resolve()) in plan.ignored_dirs[0]
        assert core_setup_hazards(project) == []

    @pytest.mark.parametrize("mode", sorted(_SYMLINK_MODES))
    def test_core_library_plan_follows_the_directory(self, tmp_path: Path, mode: str):
        project, state = _relocated_stealth_project(tmp_path, _SYMLINK_MODES[mode])
        plan = fusesoc_registry.prepare_core_library_plan(project)
        (core,) = discover_cores(project)
        operational = plan.operational_core(core)
        marker = operational.read_text(encoding="utf-8").splitlines()[1]
        assert marker.endswith(" state/cores/design.core")
        if mode == "isolated":
            assert operational.parent == (state / "tmp" / "fusesoc-isolated-cores").resolve()
            assert operational == core_projection.isolated_core_path(project, core).resolve()
        else:
            assert operational == core_projection.projected_core_path(project, core).resolve()
        assert not (project / ".booley").exists()

    def test_private_mirror_marker_names_the_relocated_core(self, tmp_path: Path):
        project, state = _relocated_stealth_project(tmp_path, _SYMLINK_MODES["isolated"])
        plan = fusesoc_registry.build_library_plan(project, tmp_path / "build")
        assert plan is not None
        mirror = plan.operational_core(state / "cores" / "design.core")
        assert (
            mirror.read_text(encoding="utf-8").splitlines()[1].endswith(" state/cores/design.core")
        )
