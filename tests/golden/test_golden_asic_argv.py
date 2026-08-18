"""Golden snapshots for the asic_synthesize sandbox argv.

What drift these protect against: the ``ca5adaf`` class — a flag emitted with
the wrong shape (a bare ``--sdc`` where run_yosys_syn expects ``--sdc <path>``)
crashed argparse deep inside the sandbox, and the substring asserts in
``tests/dev_support/test_asic_synthesize.py`` could not see the *absence/shape* of a
token.  Each test snapshots the FULL argv (one token per line) so any
added/dropped/reordered token surfaces as a diff (see
``tests/golden/conftest.py`` for the ``BOOLEY_REGEN_GOLDEN`` convention).

FuseSoC resolution is stubbed with a canned ResolvedTarget (the
``_stub_fusesoc_resolution`` autouse pattern copied from
``tests/dev_support/test_asic_synthesize.py``): ``_build_synth_cmd`` would otherwise
shell out to a real ``fusesoc run --setup``.  Every path in the resulting argv
is *relative to the work dir* (the host/sandbox-boundary contract), so the
snapshot is tmp-path-free by construction — the helper's leak guard enforces
that.

Note: ``timing_engine`` values are strictly validated host-side
(``TIMING_ENGINE_CHOICES`` = openroad/opensta/none, commit 200e7ba), so the
configs below use only valid values.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.flows.asic_synthesize import AsicSynthesizeFlow
from booley.fusesoc import fusesoc_registry
from tests.golden.conftest import assert_matches_golden

# ---------------------------------------------------------------------------
# Fixtures — copied from tests/dev_support/test_asic_synthesize.py so the golden
# module stays self-contained (that module's fixtures are file-local).
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create and return a fresh state file, set env vars."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "test-ticket"
    st.save()
    monkeypatch.setenv("BOOLEY_SLUG", "test-ticket")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
    return sf


def _fake_synth_resolved(
    work_dir: Path,
    *,
    config: str = "lite",
    toplevel: str = "dut",
) -> fusesoc_registry.ResolvedTarget:
    """A ResolvedTarget shaped like a real synth EDAM, under asic_synthesize's work root."""
    build_root = (
        Path(work_dir)
        / ".booley_project"
        / ".runtime"
        / "edalize"
        / "synth"
        / config
        / "syn_demo_0"
        / "syn"
    )
    files = (
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/include/defs.svh",
            file_type="systemVerilogSource",
            is_include=True,
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/pkg.sv",
            file_type="systemVerilogSource",
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/dut.sv",
            file_type="systemVerilogSource",
        ),
        # A file_type:SDC fileset is the post-ADR-0029 norm and (ADR 0031) the
        # thing whose absence hard-errors — so the golden Target carries one,
        # snapshotting the --sta-sdc forwarding.
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/constraints/dut.sdc",
            file_type="SDC",
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/tb/tb_dut.sv",
            file_type="systemVerilogSource",
            tags=("tb",),
        ),
    )
    params = {
        "WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 8},
        "SYNTHESIS": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
    }
    recipe: dict = {"tool": "yosys"}
    recipe_path = Path(work_dir) / ".booley_project" / "target_recipe.toml"
    if recipe_path.is_file():
        with recipe_path.open("rb") as recipe_file:
            recipe.update(tomllib.load(recipe_file).get("recipe", {}))
    return fusesoc_registry.ResolvedTarget(
        name=config,
        vlnv="::syn_demo:0",
        toplevel=toplevel,
        eda_tool="yosys",
        flow_options=recipe,
        files=files,
        parameters=params,
        build_root=build_root,
        edam_path=build_root / "syn_demo_0.eda.yml",
    )


@pytest.fixture(autouse=True)
def _stub_fusesoc_resolution(tmp_path: Path):
    """Default every test's FuseSoC resolution to a fake synth EDAM.

    Without this, ``_build_synth_cmd`` would shell out to a real
    ``fusesoc run --setup`` against a project with no ``.core`` and fail.
    """
    with patch.object(
        fusesoc_registry,
        "resolve_target",
        side_effect=lambda target="lite", **k: _fake_synth_resolved(tmp_path, config=target),
    ):
        yield


def _make_tool(tmp_path: Path, recipe_body: str | None = None) -> AsicSynthesizeFlow:
    """Build a parsed tool, optionally with Target flow-options recipe data."""
    if recipe_body is not None:
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "target_recipe.toml").write_text(
            f"[recipe]\n{recipe_body}",
            encoding="utf-8",
        )
    tool = AsicSynthesizeFlow()
    tool.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
    tool.read_state()
    return tool


# ---------------------------------------------------------------------------
# Golden argv snapshots (one token per line)
# ---------------------------------------------------------------------------


def test_synth_argv_bare_golden(state_file: Path, tmp_path: Path) -> None:
    """Snapshot the argv for a bare config (no booley.toml knobs).

    Baseline shape: standalone run_yosys_syn with resolved sources/top/params
    and no optional flags — a token appearing here unexpectedly (e.g. a bare
    ``--sdc``, the ca5adaf bug) is a diff, not a silent pass.
    """
    tool = _make_tool(tmp_path)
    cmd = tool._build_synth_cmd("lite")
    assert_matches_golden("asic_synthesize/argv_bare.txt", "\n".join(cmd))


def test_synth_argv_flatten_opensta_golden(
    state_file: Path,
    tmp_path: Path,
) -> None:
    """Snapshot the argv with every optional knob engaged.

    ``flatten = true`` → bare ``--flatten`` and ``timing_engine = "opensta"``
    → ``--timing-engine opensta`` (strictly validated host-side). SDCs come
    only from the Target fileset and are already represented by ``--sta-sdc``.
    """
    tool = _make_tool(
        tmp_path,
        'flatten = true\ntiming_engine = "opensta"\n',
    )
    cmd = tool._build_synth_cmd("lite")
    assert_matches_golden("asic_synthesize/argv_flatten_sdc_opensta.txt", "\n".join(cmd))


# NOTE: the former ``test_synth_argv_baseline_suffix_golden`` was removed —
# ``--baseline`` no longer threads a ``.baseline`` work-dir suffix through the
# argv. The baseline ref is synthesized in a separate throwaway worktree, so its
# ``-w`` result dir is physically isolated without a per-run suffix.


# ---------------------------------------------------------------------------
# Golden snapshots of the ADR 0037 configure half (generated Makefile + .ys)
# ---------------------------------------------------------------------------
# The argv above is now a *spec* — the executed command is ``make -C <rel>``
# over the build dir the configure half renders. The drift surface therefore
# moved into the generated Makefile recipes and yosys script; snapshot both.
# Every workspace path in them is build-dir-relative by construction, so the
# snapshots are tmp-path-free (the leak guard enforces it). The liberty path
# is pinned to the stable sandbox default (it crosses as an absolute path).


def _materialize_resolved_sources(tmp_path: Path, config: str = "lite") -> None:
    """Create the fake ResolvedTarget's staged files on disk.

    ``resolve_spec`` (unlike the argv builder) validates that sources exist —
    they are workspace files that must be present at configure time.
    """
    resolved = _fake_synth_resolved(tmp_path, config=config)
    for f in resolved.files:
        path = resolved.build_root / f.name
        path.parent.mkdir(parents=True, exist_ok=True)
        if f.file_type == "SDC":
            path.write_text(
                "create_clock -name clk -period 4.0 [get_ports clk]\n",
                encoding="utf-8",
            )
        else:
            path.write_text("// golden fixture\n", encoding="utf-8")


def _configure_golden_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the real configure half over the fake resolved target."""
    import dataclasses

    from booley.flows.edam import work_root_for
    from booley.yosys import run_yosys_syn, syn_make

    monkeypatch.delenv("PRJ_LIB_DIR", raising=False)
    tool = _make_tool(tmp_path, 'timing_engine = "opensta"\n')
    cmd = tool._build_synth_cmd("lite")  # rmtree's the work root — materialize after
    _materialize_resolved_sources(tmp_path)
    args = run_yosys_syn.parse_run_argv(cmd)
    spec = run_yosys_syn.resolve_spec(
        args,
        project_root=tmp_path,
        require_liberty=False,
    )
    # Pin the liberty to the stable sandbox default: the local machine's
    # DEFAULT_LIBERTY is platform-dependent and may not exist here.
    spec = dataclasses.replace(
        spec,
        liberty=Path("/opt/pdk/cell/lib/NangateOpenCellLibrary_typical_ccs.lib"),
        liberty_found=True,
    )
    build_dir = work_root_for(tmp_path, "synth", "lite") / "synth"
    return syn_make.configure_synthesis(spec, build_dir)


def test_synth_makefile_golden(
    state_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot the generated boundary Makefile (opensta engine).

    This is the executable far side of the ``make -C <rel>`` boundary command:
    a dropped stage, a lost ``BOOLEY_STAGE`` marker, or a recipe that grows a
    Booley/python dependency (contract clause c) surfaces as a diff.
    """
    plan = _configure_golden_plan(tmp_path, monkeypatch)
    makefile = (plan.build_dir / "Makefile").read_text(encoding="utf-8")
    assert_matches_golden("asic_synthesize/makefile_opensta.txt", makefile)


def test_synth_yosys_script_golden(
    state_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot the generated ``synth.ys`` (sv2v frontend, balanced ABC).

    Guards the frontend read, chparam application, netlist/stat emission and
    ABC recipe — the pieces the stdout-era tests could only substring-check.
    """
    plan = _configure_golden_plan(tmp_path, monkeypatch)
    script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")
    assert_matches_golden("asic_synthesize/synth_ys_opensta.txt", script)
