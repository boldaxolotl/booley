"""Step 12's post-setup advisories: print only what's actually outstanding.

An explicit `enabled = false` is a deliberate "Flow disabled" that must not be nagged about.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import doctor_stamp
from tests.git_command_fake import GitCommandFake
from tests.subprocess_budget import run_bounded_child

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
    (root / ".booley").mkdir(parents=True)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    return root


def _write(project: Path, toml: str, *, agents: bool = True) -> None:
    (project / ".booley" / "booley.toml").write_text(toml, encoding="utf-8")
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
        (project / ".booley" / "AGENTS.md").write_text("# guide\n", encoding="utf-8")


class SendOff(NamedTuple):
    """What a finished run prints: the advice, the summary, the exit code and the detail."""

    advice: str
    summary: str
    code: int
    detail: str | None


SendOffRunner = Callable[..., SendOff]


@pytest.fixture
def send_off(project: Path, capsys: pytest.CaptureFixture[str]) -> SendOffRunner:
    """Run the closing steps of `booley init` over a context holding the given records."""

    def run(
        *records: tuple[str, str, str], check_only: bool = False, advise: bool = True
    ) -> SendOff:
        ctx = InitContext(project_root=project, check_only=check_only)
        for record in records:
            ctx.record(*record)
        capsys.readouterr()
        # private: `run_init` reaches the closing steps only after host preparation (auth,
        # PDK cache, image reconciliation) and Sandbox Issuance, which need Docker; driving
        # each case through it takes the dozen stubs of test_init_rerun.py::repo per case.
        if advise:
            init_cmd._step_advisories(ctx)  # private: see above
        advice = capsys.readouterr().out
        code = init_cmd._print_summary(ctx)  # private: the closing step named above
        detail = next((r.detail for r in ctx.results if r.name == "advisories"), None)
        return SendOff(advice, capsys.readouterr().out, code, detail)

    return run


_ALL_STEPS = [
    *(f"Step 2 ({name})" for name in ("project config", "sim", "lint", "synth", "fpga")),
    "Step 3 (AGENTS.md)",
]
_NO_FPGA = CONFIGURED_TOML.replace("[flows.fpga]\nenabled = false\n", "")
_OUTSTANDING = {
    "fresh": (init_cmd.BOOLEY_TOML_SKELETON, False, _ALL_STEPS),
    "configured": (CONFIGURED_TOML, True, []),
    "half-finished": (
        '[project]\nname = "x"\n\n[flows.lint]\n',
        True,
        ["Step 2 (sim)", "Step 2 (synth)", "Step 2 (fpga)"],
    ),
    "unwired-fpga": (_NO_FPGA, True, ["Step 2 (fpga)"]),
    "no-agents-md": (CONFIGURED_TOML, False, ["Step 3 (AGENTS.md)"]),
    "disabled-flow": (
        CONFIGURED_TOML.replace("[flows.lint]\n", "[flows.lint]\nenabled = false\n"),
        True,
        [],
    ),
    "blank-name": (
        CONFIGURED_TOML.replace('name = "demo_cpu"', 'name = "   "'),
        True,
        ["Step 2 (project config)"],
    ),
    "unreadable": ("[project\nname = broken", False, _ALL_STEPS),
}


@pytest.mark.parametrize("case", _OUTSTANDING)
def test_send_off_lists_only_outstanding_setup_steps(
    project: Path, send_off: SendOffRunner, case: str
) -> None:
    """A half-finished project lists only its gaps (F-4: fpga_impl silent is one).

    `enabled = false` is a deliberate choice (ADR 0039), and an unparsable config is
    never reported as ready.
    """
    toml, agents, outstanding = _OUTSTANDING[case]
    _write(project, toml, agents=agents)

    off = send_off()

    assert re.findall(r"\* (Step [23] \([^)]*\))", off.advice) == outstanding
    if outstanding:
        assert "Step 0, the plan phase" in off.advice
        assert "Step 4 (doctor)" in off.advice
        assert "plans setup with you first" in off.summary
    else:
        assert off.detail == "configured"


PUBLISHED_ORIGIN = "https://github.com/boldaxolotl/booley-prj-picorv32.git"


def _git(repo: Path, *args: str) -> None:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    run_bounded_child(["git", "-C", str(repo), *identity, *args], check=True)


def _publish(project: Path, origin: str, toml: str, *, fetched: bool = True) -> None:
    """Make the Project state look like a fresh clone of *origin*.

    ``update-ref`` stands in for the fetch a real clone performs, so the
    tests need neither the network nor a second repository.
    """
    _write(project, toml)
    state_dir = project / ".booley"
    (state_dir / "scripts").mkdir()
    (state_dir / "scripts" / "build.sh").write_text("exit 0\n", encoding="utf-8")
    _git(state_dir, "init", "-q", "-b", "main")
    _git(state_dir, "remote", "add", "origin", origin)
    _git(state_dir, "add", "-A")
    _git(state_dir, "commit", "-q", "-m", "published project")
    if fetched:
        _git(state_dir, "update-ref", "refs/remotes/origin/main", "HEAD")


def test_published_probe_reports_git_execution_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch, send_off: SendOffRunner
) -> None:
    _write(project, CONFIGURED_TOML)
    GitCommandFake.install(monkeypatch).crash(
        "remote.origin.url", error=subprocess.TimeoutExpired(["git", "config"], timeout=5)
    )

    off = send_off()

    assert off.detail == "configured"
    assert "could not inspect published Project state" in off.advice
    assert "timed out" in off.advice


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/boldaxolotl/booley-prj-picorv32.git",
        "git@github.com:boldaxolotl/booley-prj-caliptra",
        "ssh://git@github.com/boldaxolotl/booley-prj-future-ip.git",
    ],
)
def test_published_project_gets_runtime_steps_instead_of_setup_skill(
    project: Path, send_off: SendOffRunner, origin: str
) -> None:
    """Any maintainer-published Project qualifies, not one hardcoded repository.

    The published toml leaves FPGA unconfigured, which the per-step probe would
    otherwise report as an outstanding booley-setup step.
    """
    _publish(project, origin, _NO_FPGA)

    off = send_off()

    out = off.advice + off.summary
    assert off.code == 0
    for fragment in (
        "preconfigured demo_cpu Project",
        "the booley-setup skill does not apply",
        "this preconfigured Project is ready",
        "open the proj folder",
        "popup notification",
        "F1",
        "Ctrl+Shift+P",
        '"Dev Containers: Reopen in Container"',
    ):
        assert fragment in out
    assert "Run the booley-setup skill" not in out
    assert "Step 2 (fpga)" not in out
    assert off.detail == "published"
    # The post-setup hook is named only when the Project ships one.
    assert "hooks/post-setup.sh" not in out
    hook = project / ".booley" / "hooks" / "post-setup.sh"
    hook.parent.mkdir()
    hook.write_bytes(b"#!/bin/sh\n")
    assert "bash .booley/hooks/post-setup.sh" in send_off().advice


#: Files a change writes into a pristine published checkout (None deletes AGENTS.md).
_CHANGES = {
    # Init appends to .gitignore and writes untracked state on every run.
    "init-owned": {".gitignore": "goals/*/\n", ".managed/project-git-hooks.pyz": ""},
    # Init records the agent choice when the published file leaves it open.
    "agent-choice": {"booley.toml": CONFIGURED_TOML + '[agent]\ndefault="claude"\nauth="api"\n'},
    # Reports and notes from ordinary use are not setup changes.
    "setup-notes": {"SETUP-NOTES.md": "x\n"},
    "edit-toml": {"booley.toml": CONFIGURED_TOML + "\n[flows.formal]\n"},
    "break-toml": {"booley.toml": "[project\n"},
    "add-core": {"cores/extra.core": "CAPI=2:\n"},
    "delete-agents": None,
    "edit-script": {"scripts/build.sh": "exit 1\n"},  # any tracked published file counts
    "commit": {"notes.md": "mine\n"},
}
_KEEPS_PUBLISHED = ["init-owned", "agent-choice", "setup-notes"]
_OTHER_ORIGINS = [
    "https://github.com/someone-else/booley-prj-picorv32.git",
    "https://github.com/boldaxolotl/booley.git",
    "https://github.com/boldaxolotl/booley-prj-.git",
    "https://example.com/github.com/boldaxolotl/booley-prj-picorv32.git",
]


def _change(state_dir: Path, change: str) -> None:
    files = _CHANGES[change]
    if files is None:
        (state_dir / "AGENTS.md").unlink()
    for name, text in (files or {}).items():
        (state_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (state_dir / name).write_text(text, encoding="utf-8")
    if change == "commit":
        _git(state_dir, "add", "notes.md")
        _git(state_dir, "commit", "-q", "-m", "local work")


@pytest.mark.parametrize(
    ("origin", "change", "fetched"),
    [
        *[pytest.param(o, None, True, id=f"origin-{n}") for n, o in enumerate(_OTHER_ORIGINS)],
        *[pytest.param(PUBLISHED_ORIGIN, c, True, id=c) for c in _CHANGES],
        # Without a remote-tracking ref nothing proves HEAD matches the origin.
        pytest.param(PUBLISHED_ORIGIN, None, False, id="unfetched"),
    ],
)
def test_only_an_unmodified_published_checkout_is_sent_to_its_runtime_steps(
    project: Path, send_off: SendOffRunner, origin: str, change: str | None, fetched: bool
) -> None:
    """A user who changed the published files owns them; the per-step probe decides."""
    _publish(project, origin, CONFIGURED_TOML, fetched=fetched)
    if change is not None:
        _change(project / ".booley", change)

    off = send_off()

    assert (off.detail == "published") is (change in _KEEPS_PUBLISHED)
    assert "could not inspect" not in off.advice


@pytest.mark.parametrize("stamp", ["none", "clean", "unreadable"])
def test_configured_project_send_off_routes_through_doctor(
    project: Path, send_off: SendOffRunner, monkeypatch: pytest.MonkeyPatch, stamp: str
) -> None:
    """Without a clean doctor stamp, "ready" still routes through verification.

    The stamp is advisory by contract: failing to read it never breaks init.
    """
    _write(project, CONFIGURED_TOML)
    if stamp == "clean":
        doctor_stamp.record_clean_run(project / ".booley", project, deep=False)
    if stamp == "unreadable":

        def gone(*_args: object, **_kwargs: object) -> None:
            raise OSError("Booley Directory is gone")

        # private: no public route makes the stamp read raise; init must survive it.
        monkeypatch.setattr(init_cmd.doctor_stamp, "check_stamp_advisory", gone)

    off = send_off()

    assert "already configured" in off.advice
    assert ("you're good to go" in off.advice) is (stamp != "none")
    assert "booley doctor" in off.advice
    assert "booley-setup skill" not in off.advice
    assert "Step 4" not in off.advice
    assert off.detail == "configured"
    assert off.code == 0
    assert "this project is ready" in off.summary
    assert "plans setup with you first" not in off.summary


def test_scaffold_send_off_is_unchanged(project: Path, send_off: SendOffRunner) -> None:
    """--scaffold keeps its own block: it writes a populated booley.toml but
    no AGENTS.md, so it is not "already configured"."""
    _write(project, CONFIGURED_TOML, agents=False)

    off = send_off(("scaffold", "ok", ""))

    assert "Scaffolded starter project" in off.advice
    assert off.detail == "scaffold"
    # Host first: the skill runs here, and it says when to Reopen in Container.
    assert "`/booley-setup new` in your agent chat here on the host" in off.advice
    assert "tells you when to Reopen in Container" in off.advice
    assert off.advice.index("/booley-setup new") < off.advice.index("Reopen in Container")
    assert 'choose "Reopen in Container"' not in off.advice


@pytest.mark.parametrize(
    ("records", "failed"),
    [
        pytest.param(
            (("docker_image", "err", "wheel build failed"),), "docker_image", id="skeleton"
        ),
        pytest.param(
            (("project_image", "err", "build failed"),), "project_image", id="configured"
        ),
        pytest.param((("line_endings", "warn", "CRLF working tree"),), None, id="warning"),
    ],
)
def test_only_a_failed_step_replaces_the_ready_send_off(
    project: Path,
    send_off: SendOffRunner,
    records: tuple[tuple[str, str, str], ...],
    failed: str | None,
) -> None:
    """fpu F-2: after a failed step the send-off must stop claiming the project is ready.

    Only `err` counts: a warning step is not a failure.
    """
    skeleton = failed == "docker_image"
    _write(
        project,
        init_cmd.BOOLEY_TOML_SKELETON if skeleton else CONFIGURED_TOML,
        agents=not skeleton,
    )

    off = send_off(*records)

    if failed is None:
        assert "already configured" in off.advice
        assert off.code == 0
        return
    assert failed in off.advice  # names what failed
    assert "NOT ready for the booley-setup skill" in off.advice
    assert "Re-run `booley init`" in off.advice
    # Even a fully configured project must not be called ready.
    assert "already configured" not in off.advice
    assert "Before running the harness" not in off.advice
    assert off.detail == "incomplete"
    assert off.code == 2
    assert "Setup incomplete" in off.summary
    assert "this project is ready" not in off.summary
    assert "plans setup with you first" not in off.summary


def test_summary_rows_use_the_shared_status_glyphs(
    project: Path, monkeypatch: pytest.MonkeyPatch, send_off: SendOffRunner
) -> None:
    from booley.harness import terminal_status as ts
    from booley.presentation import colors

    # The "Summary" banner line is matched below, so it must be plain text.
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)

    off = send_off(
        ("a", "ok", "done"),
        ("b", "skip", "not applicable"),
        ("c", "warn", "drifted"),
        ("d", "err", "broke"),
        advise=False,
    )

    lines = off.summary.splitlines()
    assert "Summary" in lines
    start = lines.index("Summary") + 1
    assert lines[start : start + 4] == [
        ts.status_line(ts.LineStatus.PASS, "a — done"),
        ts.status_line(ts.LineStatus.SKIP, "b — not applicable"),
        ts.status_line(ts.LineStatus.WARN, "c — drifted"),
        ts.status_line(ts.LineStatus.FAIL, "d — broke"),
    ]


@pytest.mark.parametrize(
    ("status", "check_only", "code", "verdict"),
    [
        ("ok", True, 0, "Checked initialization: nothing pending"),
        ("warn", True, 1, "unique-unresolved-detail"),
        ("err", True, 2, "unique-unresolved-detail"),
        # `--seed` prints no advisories, so there is nothing "above" to finish.
        ("ok", False, 0, "Booley base setup complete."),
    ],
)
def test_final_verdict_states_the_result_of_the_run(
    send_off: SendOffRunner, status: str, check_only: bool, code: int, verdict: str
) -> None:
    detail = "unique-unresolved-detail" if status != "ok" else "checked authentication"

    off = send_off(("probe", status, detail), check_only=check_only, advise=False)

    assert off.code == code
    assert verdict in (off.summary.splitlines()[-1] if check_only else off.summary)
    assert "plans setup with you first" not in off.summary


def test_configured_scaffold_stops_repeating_greenfield_setup(
    project: Path, send_off: SendOffRunner
) -> None:
    for agents in (False, True):
        for stamped in (False, True):
            _write(project, "# booley-init-origin: scaffold\n" + CONFIGURED_TOML, agents=agents)
            if stamped:
                doctor_stamp.record_clean_run(project / ".booley", project, deep=False)
            else:
                doctor_stamp.stamp_path(project / ".booley").unlink(missing_ok=True)
            output = send_off().advice
            assert "already configured" in output
            assert "/booley-setup new" not in output
            assert "AGENTS.md is\n" not in output
            assert "doctor" in output.lower()
            (project / ".booley/AGENTS.md").unlink(missing_ok=True)
    (project / "demo.core").unlink()
    output = send_off().advice
    assert "/booley-setup new" in output
    assert "every enabled Flow is wired" not in output


def test_unrecorded_legacy_projects_keep_generic_advice(
    project: Path, send_off: SendOffRunner
) -> None:
    headers = (
        "",
        "# booley.toml — generated by `booley init --scaffold` for the 'starter' starter IP.\n",
        "# generated scaffold\n",
        "# booley-init-origin-ish: scaffold\n",
    )
    for header in headers:
        for configured in (False, True):
            body = CONFIGURED_TOML if configured else init_cmd.BOOLEY_TOML_SKELETON
            _write(project, header + body, agents=configured)
            before = (project / ".booley/booley.toml").read_bytes()
            output = send_off().advice
            assert "/booley-setup new" not in output
            assert ("already configured" in output) == configured
            if configured:
                assert "Step 2 (fpga)" not in output
            else:
                assert "plan phase" in output
            assert (project / ".booley/booley.toml").read_bytes() == before
            (project / ".booley/AGENTS.md").unlink(missing_ok=True)


def test_plain_init_without_rtl_suggests_new_ip_setup(
    project: Path, send_off: SendOffRunner
) -> None:
    """No RTL or `.core`: a new IP starts with `/booley-setup new` on the host."""
    (project / ".booley" / "booley.toml").write_text(
        init_cmd.BOOLEY_TOML_SKELETON, encoding="utf-8"
    )

    off = send_off()

    out = off.advice + off.summary
    assert off.code == 0
    assert off.detail == "new-ip"
    for fragment in (
        "No RTL or `.core` files found",
        "run `/booley-setup new` in your",
        "here on the host",
        "New IP: run `/booley-setup new`",
        "Step 0, the plan phase",  # the port checklist still follows, unchanged
    ):
        assert fragment in out
    assert 'choose "Reopen in Container"' not in out
