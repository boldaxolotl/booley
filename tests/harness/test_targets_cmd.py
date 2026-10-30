"""Tests for the `booley targets` CLI verb (listing / filters / detail / --json)."""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest

from booley.fusesoc import fusesoc_registry
from booley.harness import booley as tlr
from booley.harness import colors

_CORE = textwrap.dedent(
    """\
    CAPI=2:
    name: acme:ip:alpha:1.0

    filesets:
      rtl:
        files:
          - rtl/alpha.sv: {file_type: systemVerilogSource}
      tb:
        files:
          - tb/tb_alpha.sv: {file_type: systemVerilogSource}
        tags: [tb]

    targets:
      default:
        filesets: [rtl]
      sim:
        flow: sim
        flow_options: {tool: verilator, booley: {doctor: [sim]}}
        filesets: [rtl, tb]
        toplevel: tb_alpha
      synth:
        flow: generic
        flow_options: {tool: yosys, arch: xilinx}
        filesets: [rtl]
        toplevel: alpha
      lint_selftest_bad:
        flow: lint
        flow_options: {tool: verilator, booley: {doctor_selftest: true}}
        filesets: [rtl]
    """
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "alpha.core").write_text(_CORE, encoding="utf-8")
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text("[flows.sim]\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _colors_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin plain output so FORCE_COLOR on a developer machine cannot break substrings."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)


_EXTRA_CORE = textwrap.dedent(
    """\
    CAPI=2:
    name: acme:ip:extra:1.0

    targets:
      lint_bad:
        flow: lint
        flow_options: {tool: slang}
      sim_cocotb:
        flow: sim
        flow_options: {tool: verilator, cocotb_module: tb_alpha_tests}
    """
)

_FOOTER = (
    "✓ = selected by flow_options.booley.doctor · "
    "`booley targets <name>` resolves parameters/files/SDC/XDC"
)


def _cells(line: str) -> list[str]:
    """Split a table line into cells at runs of two or more spaces."""
    return re.split(r"\s{2,}", line.strip())


def _run(project: Path, *argv: str) -> int:
    parser = tlr._build_parser()
    args = parser.parse_args(["targets", *argv])
    return tlr._cmd_targets(args, project)


class TestTargetsListing:
    def test_lists_grouped_with_doctor_selection(self, project: Path, capsys):
        assert _run(project) == 0
        out = capsys.readouterr().out
        assert "acme:ip:alpha:1.0  (alpha.core)" in out
        assert "sim" in out and "synth" in out
        assert "✓ sim" in out
        assert re.search(r"\bDr\b", out) is None

    def test_listing_has_header_groups_flow_doctor_and_footer(self, project: Path, capsys):
        (project / "x.core").write_text(_EXTRA_CORE, encoding="utf-8")
        assert _run(project) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()

        assert _cells(lines[0]) == ["TARGET", "FLOW", "EDA TOOL", "TOP", "DOCTOR"]
        assert set(lines[1].strip()) <= {"─", " "}
        assert "acme:ip:alpha:1.0  (alpha.core)" in lines
        rows = {_cells(line)[0]: _cells(line) for line in lines if line.startswith("  ")}
        assert rows["sim"] == ["sim", "sim", "verilator", "tb_alpha", "✓ sim"]
        assert rows["synth"][1] == "synth" and rows["synth"][-1] == "alpha"
        assert len(rows["synth"]) == 4  # empty DOCTOR cell leaves no cell behind
        assert rows["lint_bad"][1] == "-"
        assert "generic" not in out and "top=" not in out
        assert lines[-1] == _FOOTER
        assert lines[-2] == ""
        assert "\n\n\n" not in out

    def test_listing_styles_groups_names_and_doctor(self, project: Path, capsys, monkeypatch):
        monkeypatch.setattr(colors, "COLORS_ENABLED", True)
        assert _run(project) == 0
        out = capsys.readouterr().out
        group_line = next(line for line in out.splitlines() if "acme:ip:alpha:1.0" in line)

        assert group_line.startswith(colors.bold_accent("acme:ip:alpha:1.0"))
        assert colors.dim("(alpha.core)") in group_line
        assert colors.bold("sim") in out
        assert colors.green("✓ sim") in out
        assert colors.dim(_FOOTER) in out

    def test_piped_listing_is_aligned_plain_text(self, project: Path, capsys, monkeypatch):
        for var in ("FORCE_COLOR", "NO_COLOR"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("TERM", "xterm")
        # the real pipe decision: captured stdout is not a TTY
        monkeypatch.setattr(colors, "COLORS_ENABLED", colors._color_supported())
        (project / "x.core").write_text(_EXTRA_CORE, encoding="utf-8")
        assert _run(project) == 0
        lines = capsys.readouterr().out.splitlines()

        assert not any("\x1b" in line for line in lines)
        header = lines[0]
        assert _cells(header)[0] == "TARGET"
        starts = [header.index(h) for h in ("TARGET", "FLOW", "EDA TOOL", "TOP", "DOCTOR")]
        wanted = {"sim", "synth", "lint_bad", "sim_cocotb"}
        found = 0
        for number, line in enumerate(lines):
            if not line.startswith("  ") or line.split()[0] not in wanted:
                continue
            found += 1
            # every cell (runs of text split at 2+ spaces) starts at a header column
            for cell in re.finditer(r"\S+(?: \S+)*", line):
                assert cell.start() in starts, line
            if line.split()[0] == "sim_cocotb":
                assert lines[number + 1].strip() == "cocotb tb_alpha_tests"
        assert found == len(wanted)

    def test_no_cores_message(self, tmp_path: Path, capsys):
        assert _run(tmp_path) == 0
        assert "no .core Targets authored yet" in capsys.readouterr().out

    def test_json_listing_parses(self, project: Path, capsys):
        assert _run(project, "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert sorted(names) == ["sim", "synth"]

    # --for is a declared option string, not an argparse prefix abbreviation;
    # --for-flow remains an accepted long-form alias.
    @pytest.mark.parametrize("flag", ["--for", "--for-flow"])
    def test_for_filter(self, project: Path, capsys, flag: str):
        assert _run(project, flag, "synth", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert names == ["synth"]

    def test_for_is_declared_not_abbreviated(self):
        parser = tlr._build_parser()
        subparsers = next(a for a in parser._actions if a.dest == "command")
        targets_parser = subparsers.choices["targets"]
        for_action = next(a for a in targets_parser._actions if a.dest == "for_flow")
        assert for_action.option_strings == ["--for", "--for-flow"]

    def test_for_rejects_specialist(self, project: Path, capsys):
        assert _run(project, "--for", "reviewer") == 2
        err = capsys.readouterr().err
        assert "not a target-aware Booley Flow" in err
        assert "sim" in err  # names the valid choices

    def test_glob_positional_filters(self, project: Path, capsys):
        assert _run(project, "s?m", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert names == ["sim"]

    def test_glob_matching_nothing_is_not_an_error(self, project: Path, capsys):
        assert _run(project, "zzz*") == 0
        assert "(no Targets match)" in capsys.readouterr().out


class TestTargetsDetail:
    def test_unknown_target_is_exit_2(self, project: Path, capsys):
        assert _run(project, "ghost") == 2
        err = capsys.readouterr().err
        assert "Unknown target" in err
        assert "lint_selftest_bad" not in err

    def test_doctor_selftest_target_is_not_public(self, project: Path, capsys):
        assert _run(project, "lint_selftest_bad") == 2
        err = capsys.readouterr().err
        assert "Unknown target" in err
        assert "selectable Targets: sim, synth" in err

    def test_detail_refuses_for_filter(self, project: Path, capsys):
        assert _run(project, "sim", "--for", "sim") == 2
        assert "--for" in capsys.readouterr().err

    def test_detail_renders_cheap_half_when_fusesoc_missing(
        self, project: Path, capsys, monkeypatch
    ):
        def failing_resolve(*args, **kwargs):
            raise fusesoc_registry.TargetResolutionError("could not invoke fusesoc")

        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
        monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", failing_resolve)
        assert _run(project, "sim") == 0
        out = capsys.readouterr().out
        assert "Target sim" in out
        assert "Doctor        ✓ sim" in out
        assert "Resolved view unavailable: could not invoke fusesoc" in out
        assert "booley session enter" not in out

    def test_detail_uses_listing_palette_and_flow_meaning(
        self, project: Path, capsys, monkeypatch
    ):
        def failing_resolve(*args, **kwargs):
            raise fusesoc_registry.TargetResolutionError("no fusesoc")

        monkeypatch.setattr(colors, "COLORS_ENABLED", True)
        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
        monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", failing_resolve)

        assert _run(project, "synth") == 0
        synth = capsys.readouterr().out
        assert synth.startswith(colors.bold_accent("Target synth"))
        assert colors.dim("flow".ljust(13)) + " synth" in synth  # drivable Flow, not "generic"
        assert colors.dim(".core flow".ljust(13)) + " generic" in synth
        assert "drivable by" not in synth
        assert colors.dim("Doctor".ljust(13)) + " (not selected)" in synth

        assert _run(project, "sim") == 0
        assert colors.green("✓ sim") in capsys.readouterr().out

    def test_detail_resolved_section_is_styled(self, project: Path, capsys, monkeypatch):
        resolved = fusesoc_registry.ResolvedTarget(
            name="sim",
            vlnv="acme:ip:alpha:1.0",
            toplevel="tb_alpha",
            eda_tool=None,
            configured_eda_tool="verilator",
            files=(),
            parameters={"WIDTH": {"datatype": "int", "default": 8}, "RAW": 3},
            build_root=project / "build",
            edam_path=project / "build/demo.eda.yml",
        )
        monkeypatch.setattr(colors, "COLORS_ENABLED", True)
        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
        monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", lambda *_a, **_k: resolved)

        assert _run(project, "sim") == 0
        out = capsys.readouterr().out
        assert colors.accent("Resolved via `fusesoc run --setup`:") in out
        assert colors.strip_ansi(out).count("WIDTH (int) = 8, RAW = 3") == 1

    def test_detail_json(self, project: Path, capsys, monkeypatch):
        def failing_resolve(*args, **kwargs):
            raise fusesoc_registry.TargetResolutionError("no fusesoc")

        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
        monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", failing_resolve)
        assert _run(project, "sim", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["name"] == "sim"
        assert payload["resolved_error"] == "no fusesoc"
        assert "resolution_command" not in payload

    def test_host_detail_json_points_to_runtime_without_resolving(
        self, project: Path, capsys, monkeypatch
    ):
        def unexpected_resolve(*args, **kwargs):
            raise AssertionError("host detail must not invoke fusesoc")

        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: False)
        monkeypatch.setattr(fusesoc_registry, "_resolve_target", unexpected_resolve)

        assert _run(project, "sim", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["name"] == "sim"
        assert payload["resolved_error"] == ("detailed Target resolution requires the Sandbox")
        assert payload["resolution_command"] == (
            "booley session enter -- booley targets sim --json"
        )

    def test_host_detail_text_points_to_runtime(self, project: Path, capsys, monkeypatch):
        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: False)

        assert _run(project, "sim") == 0
        out = capsys.readouterr().out
        assert "Resolved view unavailable" in out
        assert "booley session enter -- booley targets sim" in out

    def test_help_advertises_targets(self):
        parser = tlr._build_parser()
        assert "targets" in parser.format_help()


@pytest.mark.parametrize("json_output", [False, True])
def test_lint_listing_excludes_unsupported_authored_eda_tools(
    project: Path, capsys, json_output: bool
):
    (project / "lint.core").write_text(
        "CAPI=2:\nname: acme:ip:lint:1.0\ntargets:\n"
        "  lint_canonical:\n    flow: lint\n    flow_options: {tool: verible}\n"
        "  lint_alias:\n    flow: lint\n    flow_options: {tool: veriblelint}\n"
        "  lint_unsupported:\n    flow: lint\n    flow_options: {tool: slang}\n"
        "  lint_missing:\n    flow: lint\n"
        "  lint_legacy_alias:\n    default_tool: veriblelint\n",
        encoding="utf-8",
    )
    flags = ["--json"] if json_output else []
    assert _run(project, "--for", "lint", *flags) == 0
    output = capsys.readouterr().out
    assert "lint_canonical" in output and "lint_alias" in output
    assert "lint_unsupported" not in output and "lint_missing" not in output
    assert "lint_legacy_alias" not in output
    assert _run(project, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    targets = {t["name"]: t for c in payload["cores"] for t in c["targets"]}
    for name in ("lint_unsupported", "lint_missing", "lint_legacy_alias"):
        assert "lint" not in targets[name]["drivable_by"]


@pytest.mark.parametrize("eda_tool", ["slang", "veriblelint"])
def test_lint_explicit_selection_explains_supported_eda_tools(project, eda_tool):
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import IncompatibleTargetError

    declaration = (
        "flow: lint\n    flow_options: {tool: slang}"
        if eda_tool == "slang"
        else "default_tool: veriblelint"
    )
    (project / "bad.core").write_text(
        "CAPI=2:\nname: acme:ip:bad:1.0\ntargets:\n  lint_bad:\n    " + declaration + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        IncompatibleTargetError, match=r"verilator, verible, or veriblelint.*explicit"
    ):
        TargetCatalog.build(project).select("lint_bad", for_flow="lint")


@pytest.mark.parametrize("eda_tool", ["verilator", "verible", "veriblelint"])
def test_mixed_lint_public_listing_and_selection(project, capsys, eda_tool):
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import IncompatibleTargetError

    (project / "mixed.core").write_text(
        "CAPI=2:\nname: acme:ip:mixed:1.0\ntargets:\n  lint_mixed:\n"
        f"    flow: lint\n    default_tool: {eda_tool}\n",
        encoding="utf-8",
    )
    for flags in ([], ["--json"]):
        assert _run(project, "--for", "lint", *flags) == 0
        assert "lint_mixed" not in capsys.readouterr().out
    assert _run(project, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    entry = next(t for c in payload["cores"] for t in c["targets"] if t["name"] == "lint_mixed")
    assert entry["eda_tool"] == eda_tool
    assert "lint" not in entry["drivable_by"]
    assert "lint_flow_eda_tool_missing" not in entry
    with pytest.raises(IncompatibleTargetError) as exc:
        TargetCatalog.build(project).select("lint_mixed", for_flow="lint")
    assert "booley targets --for lint" in str(exc.value)
    assert "default_tool does not supply it" in str(exc.value)


@pytest.mark.parametrize(
    "name,flow,eda_tool",
    [
        ("fpga_top", "lint", "verilator"),
        ("fpga_missing", "lint", "verible"),
        ("sim_control", "sim", "verilator"),
    ],
)
def test_lint_guidance_does_not_redeclare_other_intent(project, name, flow, eda_tool):
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import IncompatibleTargetError

    selection = (
        f"default_tool: {eda_tool}"
        if name == "fpga_missing"
        else f"flow_options: {{tool: {eda_tool}}}"
    )
    (project / "other.core").write_text(
        "CAPI=2:\nname: acme:ip:other:1.0\ntargets:\n"
        + f"  {name}:\n    flow: {flow}\n    {selection}\n"
    )
    with pytest.raises(IncompatibleTargetError) as exc:
        TargetCatalog.build(project).select(name, for_flow="lint")
    assert "booley targets --for lint" in str(exc.value)
    assert "Declare" not in str(exc.value)


def test_lint_missing_selection_guidance_does_not_invent_default(project):
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import IncompatibleTargetError

    (project / "missing.core").write_text(
        "CAPI=2:\nname: acme:ip:missing:1.0\ntargets:\n  lint_missing:\n    flow: lint\n"
    )
    with pytest.raises(IncompatibleTargetError) as exc:
        TargetCatalog.build(project).select("lint_missing", for_flow="lint")
    assert "Explicit lint requires flow_options.tool." in str(exc.value)
    assert "default_tool does not supply it" not in str(exc.value)
