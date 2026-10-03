"""Tests for the `booley targets` CLI verb (listing / filters / detail / --json)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from booley.fusesoc import fusesoc_registry
from booley.harness import booley as tlr

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
    (tmp_path / ".booley_project").mkdir()
    (tmp_path / ".booley_project" / "booley.toml").write_text("[flows.sim]\n", encoding="utf-8")
    return tmp_path


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
        assert "Dr sim" in out

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
        assert "Doctor        sim" in out
        assert "Resolved view unavailable: could not invoke fusesoc" in out
        assert "booley session enter" not in out

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
def test_lint_listing_excludes_unsupported_authored_tools(
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
    assert "lint" not in targets["lint_unsupported"]["drivable_by"]


@pytest.mark.parametrize("tool", ["slang", "veriblelint"])
def test_lint_explicit_selection_explains_supported_tools(project, tool):
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import IncompatibleTargetError

    declaration = (
        "flow: lint\n    flow_options: {tool: slang}"
        if tool == "slang"
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
