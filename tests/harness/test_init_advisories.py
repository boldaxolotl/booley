"""Step 12's post-setup advisories: print only what's actually outstanding.

Init used to end every run with the full booley-setup checklist plus "Finish the
setup skills above, then run Booley" — including on a project whose
.booley_project/ was already complete (the demo-repo clone, where the user has
nothing to finish). These tests pin the per-step probe that replaced that, and
the rule that keeps it honest: an explicit `enabled = false` is a deliberate
"Flow disabled" (Doctor reads it that way), so it must not be nagged about.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext

CONFIGURED_TOML = """\
[project]
name = "demo_cpu"

[flows]

[flows.sim]

[flows.lint]

[flows.synth]

[flows.fpga]
enabled = false
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    (root / ".booley_project").mkdir(parents=True)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    return root


def _write(project: Path, toml: str, *, agents: bool = True) -> None:
    (project / ".booley_project" / "booley.toml").write_text(toml, encoding="utf-8")
    (project / "demo.core").write_text(
        "CAPI=2:\n"
        "name: ::demo:0\n"
        "targets:\n"
        "  sim_core:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
        "  lint_core:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator, booley: {doctor: [lint]}}\n"
        "  synth_core:\n"
        "    flow: generic\n"
        "    flow_options: {tool: yosys, booley: {doctor: [synth]}}\n"
        "  fpga_core:\n"
        "    flow: generic\n"
        "    flow_options: {tool: vivado}\n",
        encoding="utf-8",
    )
    if agents:
        (project / ".booley_project" / "AGENTS.md").write_text("# guide\n", encoding="utf-8")


class TestOutstandingSteps:
    def test_fresh_init_is_outstanding_on_everything(self, project):
        _write(project, init_cmd.BOOLEY_TOML_SKELETON, agents=False)

        steps = init_cmd._outstanding_setup_steps(project)

        assert len(steps) == len(init_cmd._SETUP_STEP_LINES)
        assert any("project config" in s for s in steps) and any("synth" in s for s in steps)

    def test_fully_configured_project_has_nothing_outstanding(self, project):
        _write(project, CONFIGURED_TOML)

        assert init_cmd._outstanding_setup_steps(project) == []

    def test_half_finished_project_lists_only_the_gaps(self, project):
        """The case a fresh-vs-configured boolean would get wrong either way."""
        _write(project, '[project]\nname = "x"\n\n[flows.lint]\n')

        steps = init_cmd._outstanding_setup_steps(project)

        assert all("lint" not in s and "project config" not in s for s in steps), steps
        assert len(steps) == len(init_cmd.SETUP_WIRED_FLOWS) - 1, steps

    def test_every_wired_flow_gets_an_advisory_row(self):
        """F-4: fpga_impl -- the fourth triaged flow -- had no row at all, so a
        project that never wired it was never told. The rows are derived from
        the built-in MCP endpoint registry now, so a new flow can't go missing again."""
        keys = [key for key, _ in init_cmd._SETUP_STEP_LINES]

        assert "fpga" in keys
        assert [k for k in keys if k not in ("project", "agents")] == list(
            init_cmd.SETUP_WIRED_FLOWS
        )
        # The Elaboration Check follows [flows.sim] and has no wiring of its own.
        assert "elab" not in keys

    def test_unwired_fpga_impl_is_outstanding(self, project):
        """The F-4 report case: everything else configured, fpga_impl silent."""
        _write(project, CONFIGURED_TOML.replace("[flows.fpga]\nenabled = false\n", ""))

        steps = init_cmd._outstanding_setup_steps(project)

        assert len(steps) == 1 and "fpga" in steps[0], steps

    def test_missing_agents_md_is_outstanding(self, project):
        _write(project, CONFIGURED_TOML, agents=False)

        steps = init_cmd._outstanding_setup_steps(project)

        assert len(steps) == 1 and "Step 3" in steps[0]

    def test_explicit_enabled_false_is_a_choice_not_a_gap(self, project):
        """`enabled = false` means the Flow is deliberately disabled (ADR
        0039) — doctor skips it. Telling the user to go wire it would nag
        them into undoing their own decision."""
        _write(
            project,
            CONFIGURED_TOML.replace("[flows.lint]\n", "[flows.lint]\nenabled = false\n"),
        )

        assert init_cmd._outstanding_setup_steps(project) == []

    def test_blank_project_name_does_not_count(self, project):
        _write(project, CONFIGURED_TOML.replace('name = "demo_cpu"', 'name = "   "'))

        steps = init_cmd._outstanding_setup_steps(project)

        assert len(steps) == 1 and "project config" in steps[0]

    def test_unreadable_toml_is_treated_as_unconfigured(self, project):
        """Never claim a project is ready off a config we failed to parse."""
        _write(project, "[project\nname = broken", agents=False)

        assert len(init_cmd._outstanding_setup_steps(project)) == len(init_cmd._SETUP_STEP_LINES)


class TestAdvisorySendOff:
    def test_demo_probe_reports_git_execution_failure(self, project, monkeypatch, capsys):
        _write(project, CONFIGURED_TOML)

        def fail_git(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["git", "config"], timeout=5)

        monkeypatch.setattr(init_cmd.subprocess, "run", fail_git)

        assert init_cmd._is_demo_project(project) is False

        out = capsys.readouterr().out
        assert "could not inspect PicoRV32 demo origin" in out
        assert "timed out" in out

    def test_demo_gets_demo_next_steps_instead_of_setup_skill(self, project, capsys):
        _write(project, init_cmd.BOOLEY_TOML_SKELETON, agents=False)
        state_dir = project / ".booley_project"
        subprocess.run(["git", "init", "-q", str(state_dir)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(state_dir),
                "remote",
                "add",
                "origin",
                "https://github.com/boldaxolotl/booley-prj-picorv32.git",
            ],
            check=True,
        )
        ctx = InitContext(project_root=project)

        init_cmd._step_advisories(ctx)
        assert init_cmd._print_summary(ctx) == 0

        out = capsys.readouterr().out
        assert "Booley demo setup complete" in out
        assert '"Reopen in Container"' in out
        assert "Run the booley-setup skill" not in out
        assert ctx.results[-1].detail == "demo"

    def test_configured_project_is_not_told_to_finish_setup(self, project, capsys):
        _write(project, CONFIGURED_TOML)
        ctx = InitContext(project_root=project)

        init_cmd._step_advisories(ctx)
        out = capsys.readouterr().out

        assert "already configured" in out
        assert "booley-setup skill" not in out
        assert "Step 4" not in out
        assert ctx.results[-1].detail == "configured"

    def test_configured_project_gets_the_ready_send_off(self, project, capsys):
        _write(project, CONFIGURED_TOML)
        ctx = InitContext(project_root=project)

        init_cmd._step_advisories(ctx)
        assert init_cmd._print_summary(ctx) == 0

        out = capsys.readouterr().out
        assert "this project is ready" in out
        assert "plans setup with you first" not in out

    def test_unconfigured_project_keeps_the_checklist(self, project, capsys):
        _write(project, init_cmd.BOOLEY_TOML_SKELETON, agents=False)
        ctx = InitContext(project_root=project)

        init_cmd._step_advisories(ctx)
        assert init_cmd._print_summary(ctx) == 0

        out = capsys.readouterr().out
        assert "booley-setup skill" in out
        assert "Step 0, the plan phase" in out
        assert "Step 4 (doctor)" in out
        assert "plans setup with you first" in out

    def test_configured_project_is_nagged_to_run_doctor(self, project, capsys):
        """With no doctor stamp on record, "ready" must still route the user
        through verification rather than assert an environment nobody checked."""
        _write(project, CONFIGURED_TOML)

        init_cmd._step_advisories(InitContext(project_root=project))

        assert "booley doctor" in capsys.readouterr().out

    def test_a_clean_doctor_stamp_replaces_the_nag(self, project, capsys, monkeypatch):
        _write(project, CONFIGURED_TOML)
        monkeypatch.setattr(init_cmd.doctor_stamp, "check_stamp", lambda *a, **k: None)

        init_cmd._step_advisories(InitContext(project_root=project))

        assert "you're good to go" in capsys.readouterr().out

    def test_stamp_failure_never_breaks_init(self, project, capsys, monkeypatch):
        """The stamp is advisory by contract."""
        _write(project, CONFIGURED_TOML)

        def _boom(*a, **k):
            raise OSError("state dir is gone")

        monkeypatch.setattr(init_cmd.doctor_stamp, "check_stamp", _boom)
        ctx = InitContext(project_root=project)

        init_cmd._step_advisories(ctx)

        assert ctx.results[-1].detail == "configured"

    def test_scaffold_send_off_is_unchanged(self, project, capsys):
        """--scaffold keeps its own block: it writes a populated booley.toml but
        no AGENTS.md, so it is not "already configured"."""
        _write(project, CONFIGURED_TOML, agents=False)
        ctx = InitContext(project_root=project)
        ctx.record("scaffold", "ok", "")

        init_cmd._step_advisories(ctx)

        assert "Scaffolded starter project" in capsys.readouterr().out
        assert ctx.results[-1].detail == "scaffold"

    def test_seed_run_does_not_claim_unfinished_skills(self, capsys):
        """`--seed` prints no advisories, so there is nothing "above" to finish."""
        ctx = InitContext(project_root=Path("/tmp/x"))
        ctx.record("interactive", "ok", "app=claude")

        assert init_cmd._print_summary(ctx) == 0

        out = capsys.readouterr().out
        assert "Booley base setup complete." in out
        assert "plans setup with you first" not in out


class TestFailedStepSendOff:
    """fpu F-2: init keeps going after a failed step (every step is idempotent),
    but the send-off must stop claiming the project is ready for booley-setup."""

    def test_failed_step_replaces_the_setup_send_off(self, project, capsys):
        _write(project, init_cmd.BOOLEY_TOML_SKELETON, agents=False)
        ctx = InitContext(project_root=project)
        ctx.record("docker_image", "err", "wheel build failed")

        init_cmd._step_advisories(ctx)
        out = capsys.readouterr().out

        assert "docker_image" in out  # names what failed
        assert "NOT ready for the booley-setup skill" in out
        assert "Re-run `booley init`" in out
        # The normal "go run booley-setup" checklist must not appear.
        assert "Before running the harness" not in out
        assert "plans setup with you first" not in out
        assert ctx.results[-1].detail == "incomplete"

    def test_failed_step_outranks_the_configured_send_off(self, project, capsys):
        """Even a fully-configured project must not be called ready."""
        _write(project, CONFIGURED_TOML)
        ctx = InitContext(project_root=project)
        ctx.record("project_image", "err", "build failed")

        init_cmd._step_advisories(ctx)
        out = capsys.readouterr().out

        assert "already configured" not in out
        assert "project_image" in out

    def test_summary_still_exits_two(self, project, capsys):
        _write(project, CONFIGURED_TOML)
        ctx = InitContext(project_root=project)
        ctx.record("docker_image", "err", "wheel build failed")

        init_cmd._step_advisories(ctx)
        assert init_cmd._print_summary(ctx) == 2

        out = capsys.readouterr().out
        assert "Setup incomplete" in out
        assert "this project is ready" not in out

    def test_warnings_do_not_trigger_the_incomplete_send_off(self, project, capsys):
        """Only `err` counts — a [!!] step is not a failure."""
        _write(project, CONFIGURED_TOML)
        ctx = InitContext(project_root=project)
        ctx.record("line_endings", "warn", "CRLF working tree")

        init_cmd._step_advisories(ctx)

        assert "already configured" in capsys.readouterr().out
