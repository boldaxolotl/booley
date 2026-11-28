"""Unit tests for the non-executing Yosys synthesis configuration surface."""

from __future__ import annotations

import pytest

from booley.flows.synth.backends import configure
from booley.flows.synth.backends.configure import parse_configure_argv, resolve_spec
from booley.flows.synth.backends.yosys.parsing import parse_params
from booley.flows.target_parameters import ParameterValue


def _argv(tmp_path, *extra):
    """Parse a logical-mode configure argv over a real RTL file and Liberty file."""
    rtl = tmp_path / "dut.sv"
    rtl.write_text("module dut; endmodule\n", encoding="utf-8")
    lib = tmp_path / "cells.lib"
    lib.write_text("library (x) {}\n", encoding="utf-8")
    return parse_configure_argv(
        [
            "configure",
            "-t",
            "dut",
            "--extra-rtl",
            str(rtl),
            "--liberty",
            str(lib),
            "--synth-mode",
            "logical",
            *extra,
        ]
    )


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        # Regression: the wrapper emits one ``--extra-rtl <file>`` per source; a plain
        # ``nargs="+"`` kept only the LAST file (action="extend" must accumulate).
        (
            ["--extra-rtl", "a.sv", "--extra-rtl", "b.sv", "--extra-rtl", "c.sv"],
            {"extra_rtl": ["a.sv", "b.sv", "c.sv"]},
        ),
        (["--extra-rtl", "a.sv", "b.sv", "c.sv"], {"extra_rtl": ["a.sv", "b.sv", "c.sv"]}),
        ([], {"extra_rtl": [], "frontend": "sv2v", "slang_option": []}),
        # Regression (ravenoc halt #2b): the canonical value is itself a flag, so the
        # emitter MUST use the `=`-joined form; the two-token form is rejected.
        (
            ["--slang-option=--single-unit", "--slang-option=--allow-use-before-declare"],
            {"slang_option": ["--single-unit", "--allow-use-before-declare"]},
        ),
        (
            [
                "--ppa-profile",
                "compact",
                "--abc-recipe",
                "fast",
                "--placement-density",
                "0.72",
                "--repair-hold",
            ],
            {
                "ppa_profile": "compact",
                "abc_recipe": "fast",
                "placement_density": 0.72,
                "repair_hold": True,
            },
        ),
        (
            ["--utilization-pct", "55", "--no-repair-timing"],
            {"utilization_pct": 55.0, "repair_timing": False},
        ),
        # Absent --no-repair-timing leaves repair_timing None so TOML decides.
        ([], {"repair_timing": None, "utilization_pct": None}),
    ],
)
def test_configure_argv_parses_options(tail, expected):
    args = parse_configure_argv(["configure", "-t", "top", *tail])
    assert {key: getattr(args, key) for key in expected} == expected


def test_physical_resolve_requires_target_owned_sdc(tmp_path):
    args = _argv(tmp_path, "--synth-mode", "physical")
    with pytest.raises(SystemExit, match="requires a Target-owned"):
        resolve_spec(args, project_root=tmp_path, require_liberty=False)


def test_profile_aware_explicit_density_override_is_preserved(tmp_path):
    args = _argv(tmp_path, "--ppa-profile", "balanced", "--placement-density", "0.72")
    spec = resolve_spec(args, project_root=tmp_path)
    assert spec.timing.placement_density == 0.72


def test_conflicting_abc_controls_are_clean_config_error(tmp_path):
    args = _argv(tmp_path, "--abc-recipe", "fast", "--abc-script", "+strash")
    with pytest.raises(SystemExit, match="mutually exclusive"):
        resolve_spec(args, project_root=tmp_path)


@pytest.mark.parametrize(
    ("option", "value", "field"),
    [
        ("--utilization-pct", "nan", "utilization_pct"),
        ("--placement-density", "inf", "placement_density"),
        ("--setup-margin-ns", "nan", "setup_margin_ns"),
        ("--repair-tns-percent", "-inf", "repair_tns_percent"),
    ],
)
def test_non_finite_openroad_override_rejected(tmp_path, option, value, field):
    args = _argv(tmp_path, f"{option}={value}")
    with pytest.raises(SystemExit, match=field):
        resolve_spec(args, project_root=tmp_path)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [([], (55.0, False, None)), (["--ppa-profile", "compact"], (40.0, True, 0.65))],
)
def test_explicit_profile_replaces_legacy_timing_recipe(tmp_path, extra, expected):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        "[flows.synth.timing]\nutilization_pct = 55\nrepair_timing = false\n", encoding="utf-8"
    )
    timing = resolve_spec(_argv(tmp_path, *extra), project_root=tmp_path).timing
    assert (timing.utilization_pct, timing.repair_timing, timing.placement_density) == expected


@pytest.mark.parametrize(
    "option",
    [
        "--default-clock",
        "--clock",
        "--period-ps",
        "--input-delay-pct",
        "--output-delay-pct",
        "--max-fanout",
    ],
)
def test_removed_constraint_options_are_not_accepted(option, capsys):
    with pytest.raises(SystemExit):
        parse_configure_argv(["configure", "-t", "top", option, "4000"])
    assert f"unrecognized arguments: {option}" in capsys.readouterr().err


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Make ``do_configure`` and ``do_clean`` resolve ``tmp_path`` as the checkout."""
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    (tmp_path / ".booley").mkdir()
    return tmp_path


def test_configured_work_dirs_live_in_the_runtime_result_root(project):
    """SETUP-27: result dirs land under .booley/.runtime, not the design repo's util/syn."""
    result_root = project.resolve() / ".booley" / ".runtime" / "syn" / "syn_result"
    assert configure.do_configure(_argv(project, "--workdir", "core_a")) == result_root / "core_a"
    assert configure.do_configure(_argv(project)) == result_root / "standalone.dut"


def test_string_workdir_suffix_cannot_traverse(project):
    root = configure.do_configure(_argv(project)).parent
    for value in ["../../escape", "/absolute/path", "a\\b", "a" * 4096]:
        path = configure.do_configure(
            _argv(project, "--param", f"NAME={value}", "--param-datatype", "NAME=str")
        )
        assert path.parent == root
        assert len(path.name) < 80


def test_clean_removes_the_result_root(project, capsys):
    result_root = configure.do_configure(_argv(project)).parent
    capsys.readouterr()
    configure.do_clean()
    assert not result_root.exists()
    assert "Removed:" in capsys.readouterr().out
    configure.do_clean()
    assert "Nothing to clean." in capsys.readouterr().out


def test_declared_string_parser_preserves_whitespace():
    assert parse_params(["NAME=  ../a b  ", "N= 12 "], ["NAME=file"]) == {
        "NAME": ParameterValue("file", "  ../a b  "),
        "N": "12",
    }
