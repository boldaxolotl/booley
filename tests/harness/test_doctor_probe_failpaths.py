"""FAIL/WARN branch coverage for doctor probes that only had happy-path tests.

Each probe here had NO test driving its failure branch (2026-07-26 audit): a
regression that broke the WARN/FAIL side — or turned it into a crash — was
invisible to the suite. Every test drives the CURRENT behavior of unmodified
``doctor.py``; where that behavior is a silent return by design, the test
documents it rather than "fixing" it.

Style mirrors tests/harness/test_doctor.py: a ``_Rec`` reporter double whose
signatures exactly match ``doctor._Reporter`` (the past ``warn_(msg, fix)``
arity bug), argv-pattern subprocess fakes, tmp_path project trees.
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import booley
from booley.config import settings as harness_config
from booley.fusesoc import fusesoc_registry
from booley.harness import doctor
from booley.runtime.version_attribution import VersionAttribution, VersionOrigin


class _Rec:
    """Collects doctor check outcomes as (level, message) tuples.

    The signatures mirror ``_Reporter``'s exactly — ``warn_``/``fail_`` take an
    optional fix hint. A stub that accepted fewer args than the real reporter
    is what let a ``warn_(msg, fix)`` TypeError reach a user's `doctor` run
    (see test_doctor.py); this double is copied faithfully, not re-invented.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def p(self, m: str) -> None:
        self.events.append(("pass", m))

    def w(self, m: str, fix: str = "") -> None:
        self.events.append(("warn", m))

    def n(self, m: str) -> None:
        self.events.append(("note", m))

    def s(self, m: str) -> None:
        self.events.append(("skip", m))

    def f(self, m: str, fix: str = "") -> None:
        self.events.append(("fail", m))

    def fails(self) -> list[str]:
        return [m for lvl, m in self.events if lvl == "fail"]

    def warns(self) -> list[str]:
        return [m for lvl, m in self.events if lvl == "warn"]

    def skips(self) -> list[str]:
        return [m for lvl, m in self.events if lvl == "skip"]

    def kinds(self) -> set[str]:
        return {lvl for lvl, _ in self.events}


def _mk_audit(root: Path, booley_toml: dict | None = None) -> doctor.ProjectAudit:
    """Minimal ProjectAudit over a tmp tree (local re-implementation — the
    helpers in test_doctor.py must not be imported while parallel work edits
    that file)."""
    project_dir = root / ".booley_project"
    project_dir.mkdir(exist_ok=True)
    return doctor.ProjectAudit(
        project_root=root,
        project_dir=project_dir,
        booley_toml=booley_toml or {},
        configs_toml={"fast": {}},
        first_target="fast",
    )


# ---------------------------------------------------------------------------
# _check_agent_backend_health — WARN on any backend failure, never crash
# ---------------------------------------------------------------------------


class TestAgentBackendHealth:
    def _patch_backend(self, monkeypatch, backend) -> None:
        # doctor imports these lazily via `from booley.config.settings import
        # ...` at call time, so patching the config module attrs intercepts it.
        monkeypatch.setattr(harness_config, "load_models_config", lambda _root: None)
        monkeypatch.setattr(
            harness_config,
            "get_backend_config",
            lambda: SimpleNamespace(active_backend=backend),
        )

    def test_backend_raising_becomes_warn_not_crash(self, tmp_path, monkeypatch):
        def boom() -> str:
            raise RuntimeError("backend exploded")

        self._patch_backend(monkeypatch, SimpleNamespace(name="claude", health_check=boom))
        rec = _Rec()
        doctor._check_agent_backend_health(tmp_path, rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        assert "backend exploded" in rec.warns()[0]

    def test_config_load_failure_becomes_warn(self, tmp_path, monkeypatch):
        def bad_load(_root) -> None:
            raise ValueError("models.toml is garbage")

        monkeypatch.setattr(harness_config, "load_models_config", bad_load)
        rec = _Rec()
        doctor._check_agent_backend_health(tmp_path, rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        assert "models.toml is garbage" in rec.warns()[0]

    def test_backend_warning_string_is_relayed(self, tmp_path, monkeypatch):
        self._patch_backend(
            monkeypatch,
            SimpleNamespace(name="codex", health_check=lambda: "auth token expires soon"),
        )
        rec = _Rec()
        doctor._check_agent_backend_health(tmp_path, rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        assert "codex" in rec.warns()[0]
        assert "auth token expires soon" in rec.warns()[0]

    def test_locally_healthy_backend_is_an_offline_note(self, tmp_path, monkeypatch):
        self._patch_backend(monkeypatch, SimpleNamespace(name="claude", health_check=lambda: ""))
        rec = _Rec()
        doctor._check_agent_backend_health(tmp_path, rec.p, rec.w, _note=rec.n)
        assert rec.kinds() == {"note"}
        assert "provider authorization was not exercised" in rec.events[0][1]
        assert "--deep" in rec.events[0][1]

    # LATENT (documenting, not fixing): the except tuple is (ImportError,
    # AttributeError, RuntimeError, OSError, ValueError) — a backend whose
    # health_check raises anything else (KeyError, TypeError, ...) crashes
    # doctor instead of degrading to WARN. Pinned so a future broadening of
    # the tuple is a conscious change.
    def test_unlisted_exception_currently_escapes(self, tmp_path, monkeypatch):
        def boom() -> str:
            raise KeyError("not in the except tuple")

        self._patch_backend(monkeypatch, SimpleNamespace(name="claude", health_check=boom))
        rec = _Rec()
        with pytest.raises(KeyError):
            doctor._check_agent_backend_health(tmp_path, rec.p, rec.w)


# ---------------------------------------------------------------------------
# _check_sim_verdict_setup — sentinel WARNs + FuseSocError silent return
# ---------------------------------------------------------------------------


def _sim_ref(root: Path, cocotb_module: str | None = None) -> fusesoc_registry.TargetRef:
    return fusesoc_registry.TargetRef(
        name="sim_fast",
        vlnv="::unit:0",
        core_file=root / "unit.core",
        eda_tool="verilator",
        flow="sim",
        cocotb_module=cocotb_module,
    )


class TestSimVerdictSetup:
    def _run(self, tmp_path, tb_files, booley_toml=None, ref=None) -> _Rec:
        project = _mk_audit(tmp_path, booley_toml)
        rec = _Rec()
        doctor._check_sim_verdict_setup(
            project,
            tmp_path,
            "sim_fast",
            ref or _sim_ref(tmp_path),
            rec.p,
            rec.w,
            sources=fusesoc_registry.CoreSources(
                rtl_source_files=(),
                tb_files=tuple(tb_files),
            ),
        )
        return rec

    def test_sv_tb_without_sentinel_warns(self, tmp_path):
        (tmp_path / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")
        rec = self._run(tmp_path, ["tb.sv"])
        assert rec.kinds() == {"warn"}
        msg = rec.warns()[0]
        assert "no configured pass sentinel" in msg
        assert "$display" in msg  # SV-shaped hint, not the cocotb one

    def test_python_tb_without_sentinel_gets_cocotb_hint(self, tmp_path):
        (tmp_path / "test_dut.py").write_text("import cocotb\n", encoding="utf-8")
        rec = self._run(tmp_path, ["test_dut.py"])
        assert rec.kinds() == {"warn"}
        assert "cocotb_module" in rec.warns()[0]  # F-8: hint fits a .py TB

    def test_custom_sentinel_from_booley_toml_passes(self, tmp_path):
        (tmp_path / "tb.sv").write_text('initial $display("ALL GOOD");\n', encoding="utf-8")
        rec = self._run(
            tmp_path,
            ["tb.sv"],
            booley_toml={"flows": {"sim": {"pass_sentinels": ["ALL GOOD"]}}},
        )
        assert rec.kinds() == {"pass"}

    def test_fusesoc_error_returns_silently(self, tmp_path, monkeypatch):
        # Documenting CURRENT behavior: an enumeration failure emits nothing
        # from this probe (the structural core audit reports it separately).
        def boom(_root, _selector):
            raise fusesoc_registry.FuseSocError("core exploded")

        project = _mk_audit(tmp_path)
        monkeypatch.setattr(doctor, "inspect_target", boom)
        rec = _Rec()
        doctor._check_sim_verdict_setup(
            project, tmp_path, "sim_fast", _sim_ref(tmp_path), rec.p, rec.w
        )
        assert rec.events == []

    def test_cocotb_target_is_exempt(self, tmp_path):
        # ADR 0034 decision 6: verdict comes from results.xml — no sentinel
        # scan, no events at all.
        rec = self._run(
            tmp_path,
            ["test_dut.py"],
            ref=_sim_ref(tmp_path, cocotb_module="test_dut"),
        )
        assert rec.events == []


# ---------------------------------------------------------------------------
# _audit_tests_toml_targets — dead section key FAIL + FuseSocError return
# ---------------------------------------------------------------------------


class TestAuditTestsTomlTargets:
    def _refs(self, root: Path) -> dict[str, fusesoc_registry.TargetRef]:
        return {"sim_fast": _sim_ref(root)}

    def test_dead_section_key_fails(self, tmp_path, monkeypatch):
        project = _mk_audit(tmp_path)
        monkeypatch.setattr(
            doctor.fusesoc_registry, "enumerate_targets", lambda _root: self._refs(tmp_path)
        )
        rec = _Rec()
        doctor._audit_tests_toml_targets(project, {"sim_ghost": {}}, rec.f)
        assert len(rec.fails()) == 1
        assert "[sim_ghost]" in rec.fails()[0]
        assert "no declared .core Target" in rec.fails()[0]

    def test_bare_and_qualified_keys_resolve(self, tmp_path, monkeypatch):
        project = _mk_audit(tmp_path)
        monkeypatch.setattr(
            doctor.fusesoc_registry, "enumerate_targets", lambda _root: self._refs(tmp_path)
        )
        rec = _Rec()
        sections = {"sim_fast": {}, "::unit:0#sim_fast": {}}
        doctor._audit_tests_toml_targets(project, sections, rec.f)
        assert rec.events == []

    def test_fusesoc_error_returns_silently(self, tmp_path, monkeypatch):
        # Documenting CURRENT behavior: enumeration failure emits nothing here
        # (the structural audit already reported it).
        def boom(_root):
            raise fusesoc_registry.FuseSocError("no cores")

        project = _mk_audit(tmp_path)
        monkeypatch.setattr(doctor.fusesoc_registry, "enumerate_targets", boom)
        rec = _Rec()
        doctor._audit_tests_toml_targets(project, {"sim_ghost": {}}, rec.f)
        assert rec.events == []


# ---------------------------------------------------------------------------
# _check_custom_endpoints_and_criteria — both FAIL branches
# ---------------------------------------------------------------------------


class TestCustomToolsAndCriteria:
    def test_preflight_error_fails_with_first_failure_as_fix(self, tmp_path, monkeypatch):
        from booley.harness import preflight

        def boom(_root):
            raise preflight.PreflightError(["MCP endpoint 'x' has no command", "second"])

        monkeypatch.setattr(preflight, "_validate_custom_endpoints_and_criteria", boom)
        rec = _Rec()
        fixes: list[str] = []

        def fail_with_fix(m: str, fix: str = "") -> None:
            rec.f(m, fix)
            fixes.append(fix)

        doctor._check_custom_endpoints_and_criteria(tmp_path, rec.p, fail_with_fix)
        assert rec.kinds() == {"fail"}
        assert "custom endpoint/Criteria validation failed" in rec.fails()[0]
        # The first PreflightError failure is surfaced as the fix hint.
        assert fixes == ["MCP endpoint 'x' has no command"]

    def test_generic_error_fails_with_message(self, tmp_path, monkeypatch):
        from booley.harness import preflight

        def boom(_root):
            raise ValueError("criteria.toml is not a table")

        monkeypatch.setattr(preflight, "_validate_custom_endpoints_and_criteria", boom)
        rec = _Rec()
        doctor._check_custom_endpoints_and_criteria(tmp_path, rec.p, rec.f)
        assert rec.kinds() == {"fail"}
        assert "criteria.toml is not a table" in rec.fails()[0]


# ---------------------------------------------------------------------------
# _check_git_conflicts + _warn_if_dirty — real git repos in tmp_path
# ---------------------------------------------------------------------------


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, text=True)


class TestGitConflictsAndDirty:
    def test_merge_in_progress_fails(self, tmp_path):
        _git_init(tmp_path)
        # A MERGE_HEAD in .git is exactly what an unfinished merge leaves.
        (tmp_path / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
        rec = _Rec()
        doctor._check_git_conflicts(tmp_path, rec.p, rec.f)
        assert rec.kinds() == {"fail"}
        assert "git operation in progress" in rec.fails()[0]
        assert "merge" in rec.fails()[0]

    def test_clean_repo_passes(self, tmp_path):
        _git_init(tmp_path)
        rec = _Rec()
        doctor._check_git_conflicts(tmp_path, rec.p, rec.f)
        assert rec.kinds() == {"pass"}

    def test_dirty_tree_notes_count(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("y\n", encoding="utf-8")
        rec = _Rec()
        doctor._note_if_dirty(tmp_path, rec.n)
        assert rec.kinds() == {"note"}
        assert "2 modified file(s)" in rec.events[0][1]

    def test_clean_tree_stays_silent(self, tmp_path):
        _git_init(tmp_path)
        rec = _Rec()
        doctor._note_if_dirty(tmp_path, rec.n)
        assert rec.events == []


# ---------------------------------------------------------------------------
# _check_interactive_docker_objects — WARN branches (ADR 0018 objects)
# ---------------------------------------------------------------------------


class TestInteractiveDockerObjects:
    def test_no_runtime_skips(self):
        rec = _Rec()
        doctor._check_interactive_docker_objects(None, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"skip"}

    def test_everything_missing_warns_three_times(self, monkeypatch):
        monkeypatch.setattr(doctor.idk, "network_exists", lambda *a, **k: False)
        monkeypatch.setattr(doctor.idk, "container_running", lambda _name: False)
        monkeypatch.setattr(doctor.idk, "container_exists", lambda _name: False)
        rec = _Rec()
        doctor._check_interactive_docker_objects("docker", rec.p, rec.w, rec.s)
        warns = rec.warns()
        assert rec.kinds() == {"warn"}
        assert len(warns) == 3  # network + proxy + reaper
        assert any("network missing" in w for w in warns)
        assert sum("missing - run booley init" in w for w in warns) >= 2

    def test_leaky_network_and_stopped_containers_warn(self, monkeypatch):
        monkeypatch.setattr(doctor.idk, "network_exists", lambda *a, **k: True)
        monkeypatch.setattr(doctor.idk, "network_is_internal", lambda *a, **k: False)
        monkeypatch.setattr(doctor.idk, "network_is_host_isolated", lambda *a, **k: False)
        monkeypatch.setattr(doctor.idk, "container_running", lambda _name: False)
        monkeypatch.setattr(doctor.idk, "container_exists", lambda _name: True)
        rec = _Rec()
        doctor._check_interactive_docker_objects("docker", rec.p, rec.w, rec.s)
        warns = rec.warns()
        assert any("not both --internal and host-isolated" in w for w in warns)
        assert sum("stopped" in w for w in warns) == 2

    def test_healthy_objects_pass(self, monkeypatch):
        monkeypatch.setattr(doctor.idk, "network_exists", lambda *a, **k: True)
        monkeypatch.setattr(doctor.idk, "network_is_internal", lambda *a, **k: True)
        monkeypatch.setattr(doctor.idk, "network_is_host_isolated", lambda *a, **k: True)
        monkeypatch.setattr(doctor.idk, "container_running", lambda _name: True)
        rec = _Rec()
        doctor._check_interactive_docker_objects("docker", rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}


# ---------------------------------------------------------------------------
# _check_legacy_distribution — both FAIL branches (pre-rename `booley` dist)
# ---------------------------------------------------------------------------


class _FakeDist:
    metadata: ClassVar[dict[str, str]] = {"Version": "0.0.9"}


class TestLegacyDistribution:
    def _patch_dist_present(self, monkeypatch, *, current: bool) -> None:
        # The probe does `from importlib.metadata import distribution` at call
        # time, so patching the module attribute intercepts it.
        def distribution(name):
            if name == "booley" or current:
                return _FakeDist()
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", distribution)

    def test_legacy_only_install_fails(self, monkeypatch):
        self._patch_dist_present(monkeypatch, current=False)
        monkeypatch.setattr(booley, "__dist_name__", "booley")
        monkeypatch.setattr(
            booley,
            "version_attribution",
            VersionAttribution(
                version="0.0.9",
                origin=VersionOrigin.DISTRIBUTION,
                distribution_name="booley",
            ),
        )
        rec = _Rec()
        doctor._check_legacy_distribution(rec.p, rec.f)
        assert rec.kinds() == {"fail"}
        assert "booley-rtl` is not installed at all" in rec.fails()[0]
        assert "0.0.9" in rec.fails()[0]

    def test_both_installed_fails_on_shadowing(self, monkeypatch):
        self._patch_dist_present(monkeypatch, current=True)
        monkeypatch.setattr(booley, "__dist_name__", "booley-rtl")
        monkeypatch.setattr(
            booley,
            "version_attribution",
            VersionAttribution(
                version="1.0.0",
                origin=VersionOrigin.DISTRIBUTION,
                distribution_name="booley-rtl",
            ),
        )
        rec = _Rec()
        doctor._check_legacy_distribution(rec.p, rec.f)
        assert rec.kinds() == {"fail"}
        assert "both `booley`" in rec.fails()[0]
        assert "shadow" in rec.fails()[0]

    def test_no_legacy_dist_passes(self, monkeypatch):
        def not_found(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", not_found)
        rec = _Rec()
        doctor._check_legacy_distribution(rec.p, rec.f)
        assert rec.kinds() == {"pass"}


# ---------------------------------------------------------------------------
# _check_skills — empty-but-present dir downgrades FAIL to WARN
# ---------------------------------------------------------------------------


class TestSkills:
    def test_existing_dir_with_zero_subdirs_warns_not_fails(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        skills = home / doctor._SKILL_DIRS[0]
        skills.mkdir(parents=True)
        # A stray FILE must not count as a skill — only subdirectories do.
        (skills / "README.md").write_text("not a skill\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)
        rec = _Rec()
        doctor._check_skills(rec.p, rec.w, rec.f)
        assert rec.kinds() == {"warn"}  # downgraded: dir exists, just empty
        assert "no skills in" in rec.warns()[0]

    def test_no_skills_dir_at_all_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        rec = _Rec()
        doctor._check_skills(rec.p, rec.w, rec.f)
        assert rec.kinds() == {"fail"}
        assert "no system-level skills directory" in rec.fails()[0]

    # LATENT (documenting, not fixing): the loop RETURNS on the first existing
    # dir, so an empty first-priority dir WARNs even when the second dir is
    # fully populated — the populated one is never consulted.
    def test_empty_first_dir_shadows_populated_second(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / doctor._SKILL_DIRS[0]).mkdir(parents=True)  # empty
        (home / doctor._SKILL_DIRS[1] / "my-skill").mkdir(parents=True)  # populated
        monkeypatch.setattr(Path, "home", lambda: home)
        rec = _Rec()
        doctor._check_skills(rec.p, rec.w, rec.f)
        assert rec.kinds() == {"warn"}  # current behavior: second dir ignored


# ---------------------------------------------------------------------------
# _check_image_bakes_runtime_marker — WARN on missing marker + SKIP paths
# ---------------------------------------------------------------------------


class TestImageBakesVenueMarker:
    _IMAGE = "unit-sandbox:latest"

    def _patch_inspect(self, monkeypatch, *, returncode: int, env: list | None):
        def fake_run(cmd, **kwargs):
            assert cmd[1:3] == ["image", "inspect"], f"unexpected argv: {cmd}"
            stdout = json.dumps(env) if env is not None else ""
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    def test_marker_absent_warns(self, monkeypatch):
        self._patch_inspect(monkeypatch, returncode=0, env=["PATH=/usr/bin", "LANG=C.UTF-8"])
        rec = _Rec()
        doctor._check_image_bakes_runtime_marker("docker", self._IMAGE, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"warn"}
        assert "does not bake BOOLEY_CONTAINER=1" in rec.warns()[0]

    def test_no_runtime_skips(self):
        rec = _Rec()
        doctor._check_image_bakes_runtime_marker(None, self._IMAGE, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"skip"}
        assert "container runtime unavailable" in rec.skips()[0]

    def test_missing_image_skips(self, monkeypatch):
        self._patch_inspect(monkeypatch, returncode=1, env=None)
        rec = _Rec()
        doctor._check_image_bakes_runtime_marker("docker", self._IMAGE, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"skip"}
        assert "not present" in rec.skips()[0]

    def test_marker_present_passes(self, monkeypatch):
        self._patch_inspect(monkeypatch, returncode=0, env=["BOOLEY_CONTAINER=1"])
        rec = _Rec()
        doctor._check_image_bakes_runtime_marker("docker", self._IMAGE, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}
