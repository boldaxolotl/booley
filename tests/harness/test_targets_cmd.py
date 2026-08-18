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
        flow_options: {tool: verilator}
        filesets: [rtl, tb]
        toplevel: tb_alpha
      synth:
        flow: generic
        flow_options: {tool: yosys, arch: xilinx}
        filesets: [rtl]
        toplevel: alpha
    """
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "alpha.core").write_text(_CORE, encoding="utf-8")
    (tmp_path / ".booley_project").mkdir()
    (tmp_path / ".booley_project" / "booley.toml").write_text(
        '[flows.sim]\ndefault_target = "sim"\n', encoding="utf-8"
    )
    return tmp_path


def _run(project: Path, *argv: str) -> int:
    parser = tlr._build_parser()
    args = parser.parse_args(["targets", *argv])
    return tlr._cmd_targets(args, project)


class TestTargetsListing:
    def test_lists_grouped_with_wiring(self, project: Path, capsys):
        assert _run(project) == 0
        out = capsys.readouterr().out
        assert "acme:ip:alpha:1.0  (alpha.core)" in out
        assert "sim" in out and "synth" in out
        assert "← sim" in out

    def test_no_cores_message(self, tmp_path: Path, capsys):
        assert _run(tmp_path) == 0
        assert "no .core Targets authored yet" in capsys.readouterr().out

    def test_json_listing_parses(self, project: Path, capsys):
        assert _run(project, "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert sorted(names) == ["sim", "synth"]

    def test_for_filter(self, project: Path, capsys):
        assert _run(project, "--for", "synth", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert names == ["synth"]

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
        assert "Unknown target" in capsys.readouterr().err

    def test_detail_refuses_for_filter(self, project: Path, capsys):
        assert _run(project, "sim", "--for", "sim") == 2
        assert "--for" in capsys.readouterr().err

    def test_detail_renders_cheap_half_when_fusesoc_missing(
        self, project: Path, capsys, monkeypatch
    ):
        def failing_resolve(*args, **kwargs):
            raise fusesoc_registry.TargetResolutionError("could not invoke fusesoc")

        monkeypatch.setattr(fusesoc_registry, "resolve_target", failing_resolve)
        assert _run(project, "sim") == 0
        out = capsys.readouterr().out
        assert "Target sim" in out
        assert "wired to      sim" in out
        assert "Resolved view unavailable: could not invoke fusesoc" in out

    def test_detail_json(self, project: Path, capsys, monkeypatch):
        def failing_resolve(*args, **kwargs):
            raise fusesoc_registry.TargetResolutionError("no fusesoc")

        monkeypatch.setattr(fusesoc_registry, "resolve_target", failing_resolve)
        assert _run(project, "sim", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["name"] == "sim"
        assert payload["resolved_error"] == "no fusesoc"

    def test_help_advertises_targets(self):
        parser = tlr._build_parser()
        assert "targets" in parser.format_help()
