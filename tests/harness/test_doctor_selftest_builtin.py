"""--deep fail-path self-test must run on the BUILTIN flow (ADR 0039).

The QA-4/QA-5 fixture mechanism was gated `is_project_native`, so a builtin
project's conventional fail-path fixtures silently never ran. This was found
by the C910 re-port gate, where doctor --deep reported green without ever
proving the fail path. The gate is now backend-agnostic: only a disabled Flow
(enabled = false) skips.
"""

from __future__ import annotations

import subprocess

from booley import runtime_context, selftest_overlay
from booley.harness import doctor


class _Rec:
    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def p(self, msg):
        self.events.append(("pass", msg))

    def w(self, msg):
        self.events.append(("warn", msg))

    def s(self, msg):
        self.events.append(("skip", msg))

    def f(self, msg, fix=""):
        self.events.append(("fail", msg))

    def fails(self):
        return [m for lvl, m in self.events if lvl == "fail"]


def _set_venue(monkeypatch, inside: bool) -> None:
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: inside)


def _audit(
    tmp_path,
    *,
    enabled: bool | None = None,
    fixture: bool = True,
):
    pd = tmp_path / ".booley_project"
    pd.mkdir(exist_ok=True)
    sim: dict = {"default_target": "sim_core"}
    if enabled is not None:
        sim["enabled"] = enabled
    (pd / "tests.toml").write_text('[sim_core]\ntests = ["hello_world"]\n', encoding="utf-8")
    if fixture:
        overlay = pd / "selftest" / "sim" / "bad-overlay" / "firmware.hex"
        overlay.parent.mkdir(parents=True)
        overlay.write_text("broken\n", encoding="utf-8")
    return doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=pd,
        booley_toml={"flows": {"sim": sim, "lint": {"enabled": False}}},
        configs_toml={"sim_core": {}},
        first_target="sim_core",
    )


def _run(monkeypatch, project, exit_by_kind):
    # In-container in-place execution path (F-17): no docker wrap needed.
    _set_venue(monkeypatch, True)

    def fake_run(cmd, **kwargs):
        kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
        return subprocess.CompletedProcess(cmd, exit_by_kind[kind], stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    rec = _Rec()
    doctor._run_selftest_checks(project, None, rec.p, rec.w, rec.s, rec.f)
    return rec


def test_builtin_selftest_runs(tmp_path, monkeypatch):
    # Default backend (builtin, no key at all) with conventional fixtures → the
    # good/bad pair executes and grades.
    rec = _run(monkeypatch, _audit(tmp_path), {"good": 0, "bad": 1})
    assert not rec.fails()
    assert any("correctly graded a failure" in m for _, m in rec.events)


def test_builtin_false_pass_is_caught(tmp_path, monkeypatch):
    rec = _run(monkeypatch, _audit(tmp_path), {"good": 0, "bad": 0})
    assert any("FALSE-PASSED" in m for m in rec.fails())


def test_good_and_bad_runs_pin_the_same_smoke_test(tmp_path, monkeypatch):
    seen_tests: list[str] = []
    _set_venue(monkeypatch, True)

    def fake_run(cmd, **kwargs):
        seen_tests.append(cmd[cmd.index("--test") + 1])
        kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
        return subprocess.CompletedProcess(cmd, {"good": 0, "bad": 1}[kind], stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    rec = _Rec()
    doctor._run_selftest_checks(_audit(tmp_path), None, rec.p, rec.w, rec.s, rec.f)

    assert not rec.fails()
    assert seen_tests == ["hello_world", "hello_world"]


def test_builtin_without_fixtures_warns(tmp_path, monkeypatch):
    _set_venue(monkeypatch, False)
    rec = _Rec()
    doctor._run_selftest_checks(
        _audit(tmp_path, fixture=False), "docker", rec.p, rec.w, rec.s, rec.f
    )
    assert not rec.fails()
    assert any("fail-path unvalidated" in m for lvl, m in rec.events if lvl == "warn")


def _lint_audit(tmp_path, *, fixture: bool = True):
    pd = tmp_path / ".booley_project"
    pd.mkdir(exist_ok=True)
    bad_target = (
        "  lint_selftest_bad:\n    flow: lint\n    flow_options: {tool: verilator}\n"
        if fixture
        else ""
    )
    (tmp_path / "unit.core").write_text(
        "CAPI=2:\n"
        "name: booley::unit:0\n"
        "targets:\n"
        "  lint_core:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        f"{bad_target}",
        encoding="utf-8",
    )
    return doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=pd,
        booley_toml={
            "flows": {
                "sim": {"enabled": False},
                "lint": {"default_target": "lint_core"},
            }
        },
        configs_toml={},
        first_target="",
    )


def test_lint_selftest_uses_conventional_bad_target(tmp_path, monkeypatch):
    seen_targets: list[str] = []
    _set_venue(monkeypatch, True)

    def fake_run(cmd, **kwargs):
        seen_targets.append(cmd[cmd.index("--target") + 1])
        kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
        return subprocess.CompletedProcess(cmd, {"good": 0, "bad": 1}[kind], stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    rec = _Rec()
    doctor._run_selftest_checks(_lint_audit(tmp_path), None, rec.p, rec.w, rec.s, rec.f)

    assert not rec.fails()
    assert seen_targets == ["lint_core", "lint_selftest_bad"]


def test_lint_without_conventional_bad_target_warns(tmp_path, monkeypatch):
    _set_venue(monkeypatch, False)
    rec = _Rec()

    doctor._run_selftest_checks(
        _lint_audit(tmp_path, fixture=False), "docker", rec.p, rec.w, rec.s, rec.f
    )

    warns = [m for level, m in rec.events if level == "warn"]
    lint_warn = next(m for m in warns if m.startswith("lint fail-path unvalidated"))
    assert "lint_selftest_bad" in lint_warn
    assert "committed to the repo" in lint_warn


def test_disabled_flow_skips_silently(tmp_path, monkeypatch):
    _set_venue(monkeypatch, False)
    rec = _Rec()
    doctor._run_selftest_checks(
        _audit(tmp_path, enabled=False), "docker", rec.p, rec.w, rec.s, rec.f
    )
    # The disabled simulate emits nothing — not even the fixtures nag. (The
    # unconfigured-but-active lint still gets its own unvalidated WARN.)
    assert not rec.fails()
    assert not any("sim" in m for _, m in rec.events)


def test_unvalidated_warning_names_the_sim_footprint_tradeoff(tmp_path, monkeypatch):
    """fpu F-20: enabling a selftest means putting a known-bad fixture
    somewhere, and nothing said so — the advisory read as a free win, so the
    port hand-proved its fail paths and left the WARN standing, unexplained."""
    _set_venue(monkeypatch, False)
    rec = _Rec()

    doctor._run_selftest_checks(
        _audit(tmp_path, fixture=False), "docker", rec.p, rec.w, rec.s, rec.f
    )

    warns = [m for lvl, m in rec.events if lvl == "warn"]
    sim_warn = next(m for m in warns if m.startswith("sim fail-path unvalidated"))
    # Simulation's project-owned overlay stays out of public Flow behavior.
    assert "Footprint:" in sim_warn
    assert ".booley_project/selftest/sim/bad-overlay/" in sim_warn


def test_footprint_note_exists_for_every_selftest_tool():
    """A Flow added to the self-test registry without a note would silently lose it."""
    assert set(doctor._SELFTEST_FLOWS) <= set(doctor._SELFTEST_FOOTPRINT_NOTE)
