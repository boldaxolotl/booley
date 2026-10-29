"""Migrated readiness diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness.setup import readiness
from booley.harness.setup.project_origin import record_scaffold_origin
from booley.harness.setup.scaffold import ScaffoldChoices, scaffold_files
from booley.runtime.booley_dir import reset_cache, resolve_booley_dir
from tests.diagnostic_helpers import (
    _Rec,
    _record_report,
)
from tests.harness.test_doctor import _write_project


@pytest.fixture(autouse=True)
def _isolate_project_cache():
    """Each readiness test controls its Project selection independently."""
    reset_cache()
    yield
    reset_cache()


def test_doctor_prefers_linked_checkout_project_snapshot(tmp_path, monkeypatch):
    """F-25: Doctor config and design inputs must share one checkout."""
    canonical_root = tmp_path / "canonical"
    checkout_root = tmp_path / "ticket-checkout"
    canonical_root.mkdir()
    checkout_root.mkdir()
    canonical_dir = _write_project(canonical_root)
    checkout_dir = _write_project(checkout_root)
    checkout_toml = checkout_dir / "booley.toml"
    checkout_toml.write_text(
        checkout_toml.read_text(encoding="utf-8").replace('name = "unit"', 'name = "ticket"'),
        encoding="utf-8",
    )
    reset_cache()
    monkeypatch.setenv("BOOLEY_DIR", str(canonical_dir))
    assert resolve_booley_dir() == canonical_dir.resolve()  # pre-warm session-global cache

    audit = readiness.load_project(checkout_root).project

    assert audit is not None
    assert audit.booley_dir == checkout_dir.resolve()
    assert audit.booley_toml["project"]["name"] == "ticket"


class TestStealthCoresCheck:
    """ADR 0036 contract: authored cores live in .booley/cores/ (and
    nowhere else in the Booley Directory), and never share a VLNV with a repo core."""

    def _project(self, tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "repo"
        booley_dir = root / ".booley"
        (booley_dir / "cores").mkdir(parents=True)
        return root, booley_dir

    def test_clean_layout_passes(self, tmp_path: Path):
        root, booley_dir = self._project(tmp_path)
        (root / "repo.core").write_text("CAPI=2:\nname: ::repo:0\n", encoding="utf-8")
        (booley_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        (booley_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, booley_dir, {"stealth": {"enabled": True}}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert rec.kinds() == {"pass"}

    def test_stranded_core_fails_with_move_hint(self, tmp_path: Path):
        # The original stealth-project layout: authored cores directly under
        # .booley/ — structurally skipped, so Targets silently vanish.
        root, booley_dir = self._project(tmp_path)
        (booley_dir / "stranded.core").write_text("CAPI=2:\nname: ::s:0\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, booley_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert "stranded" in rec.fails()[0]
        assert "stranded.core" in rec.fails()[0]

    def test_worktree_copies_are_not_stranded(self, tmp_path: Path):
        root, booley_dir = self._project(tmp_path)
        wt = booley_dir / "worktrees" / "t1"
        wt.mkdir(parents=True)
        (wt / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        bl = booley_dir / ".baseline-wt-7-abc"
        bl.mkdir()
        (bl / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, booley_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert rec.kinds() == {"pass"}

    def test_private_registry_copies_are_not_stranded(self, tmp_path: Path):
        root, booley_dir = self._project(tmp_path)
        registry = booley_dir / "tmp" / "fusesoc-isolated-cores"
        registry.mkdir(parents=True)
        (registry / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        rec = _Rec()

        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, booley_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )

        assert rec.kinds() == {"pass"}

    def test_cross_root_collision_fails(self, tmp_path: Path):
        root, booley_dir = self._project(tmp_path)
        (root / "repo.core").write_text("CAPI=2:\nname: ::dup:0\n", encoding="utf-8")
        (booley_dir / "cores" / "s.core").write_text("CAPI=2:\nname: ::dup:1\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, booley_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert any("both core roots" in msg for msg in rec.fails())

    def test_hidden_cores_require_stealth_mode(self, tmp_path: Path):
        root, booley_dir = self._project(tmp_path)
        (booley_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        rec = _Rec()

        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, booley_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )

        assert any("stealth mode is disabled" in msg for msg in rec.fails())

    def test_stealth_projection_is_repaired(self, tmp_path: Path):
        root, booley_dir = self._project(tmp_path)
        (booley_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        (booley_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        rec = _Rec()

        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, booley_dir, {"stealth": {"enabled": True}}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            failed=rec.f,
        )

        assert rec.fails() == []
        assert (root / ".booley-projected-s.core").is_file()


_SCAFFOLD_CHECK = "stealth.scaffold-core-tracked"


def _commit_all(root: Path) -> None:
    for args in (["init", "-q"], ["add", "-f", "."], ["commit", "-qm", "scaffold"]):
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=10,
        )


def _scaffold_core_findings(root: Path):
    audit = readiness.ProjectAudit(root, root / ".booley", {"stealth": {"enabled": True}}, {}, "")
    report = readiness.check_stealth_cores(audit, mode=readiness.ReadinessMode.INSPECT)
    return [f for f in report.findings if f.check_id == _SCAFFOLD_CHECK]


def _write_scaffold(root: Path) -> None:
    choices = ScaffoldChoices("my_ip", "verilator", "sv", "verilator", False, None)
    for rel, content in scaffold_files(choices).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    record_scaffold_origin(root / ".booley/booley.toml")


class TestScaffoldRootCoresTracked:
    """A scaffold Project switched to Stealth Mode with root cores still tracked."""

    def test_tracked_scaffold_cores_warn_in_stealth(self, tmp_path: Path, monkeypatch):
        _write_scaffold(tmp_path)
        _commit_all(tmp_path)
        # "Switched to Stealth Mode" after the cores were committed.
        toml = tmp_path / ".booley/booley.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8").replace("enabled = false", "enabled = true"),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path.parent)  # relative git paths must not read from cwd

        findings = _scaffold_core_findings(tmp_path)

        assert {f.subject for f in findings} == {"my_ip.core", "verif/booley_doctor_selftest.core"}
        assert all(".booley/cores/" in f.fix for f in findings)
        assert all(f.subject in f.message for f in findings)

    @pytest.mark.parametrize("case", ["native-core", "port-origin", "projection"])
    def test_native_root_core_not_flagged(self, tmp_path: Path, case: str):
        _write_scaffold(tmp_path)
        for rel in ("my_ip.core", "verif/booley_doctor_selftest.core"):
            (tmp_path / rel).unlink()
        scaffold_text = "CAPI=2:\nname: ::my_ip:0\ndescription: {}\n"
        if case == "native-core":
            (tmp_path / "ip.core").write_text(scaffold_text.format("A hand-written IP"))
        elif case == "port-origin":
            toml = tmp_path / ".booley/booley.toml"
            toml.write_text(
                toml.read_text(encoding="utf-8").replace(
                    "# booley-init-origin: scaffold", "# booley-init-origin: port"
                ),
                encoding="utf-8",
            )
            (tmp_path / "my_ip.core").write_text(
                scaffold_text.format("my_ip starter IP (scaffolded by booley init --scaffold)")
            )
        else:
            (tmp_path / ".booley-projected-my_ip.core").write_text(
                scaffold_text.format("my_ip starter IP (scaffolded by booley init --scaffold)")
            )
        _commit_all(tmp_path)

        assert _scaffold_core_findings(tmp_path) == []

    def test_no_findings_outside_a_git_repository(self, tmp_path: Path):
        _write_scaffold(tmp_path)  # never committed: git cannot say what is tracked

        assert _scaffold_core_findings(tmp_path) == []

    def test_no_findings_when_git_cannot_run(self, tmp_path: Path, monkeypatch):
        _write_scaffold(tmp_path)
        _commit_all(tmp_path)

        def _missing_git(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(readiness.subprocess, "run", _missing_git)

        assert _scaffold_core_findings(tmp_path) == []

    def test_unreadable_tracked_core_is_left_to_the_core_audit(self, tmp_path: Path):
        _write_scaffold(tmp_path)
        (tmp_path / "my_ip.core").write_text("CAPI=2:\nname: [unclosed\n", encoding="utf-8")
        (tmp_path / "verif/booley_doctor_selftest.core").unlink()
        _commit_all(tmp_path)

        assert _scaffold_core_findings(tmp_path) == []

    def test_relocated_stealth_core_is_not_flagged(self, tmp_path: Path):
        _write_scaffold(tmp_path)
        cores = tmp_path / ".booley/cores"
        cores.mkdir(parents=True)
        (tmp_path / "my_ip.core").rename(cores / "my_ip.core")
        (tmp_path / "verif/booley_doctor_selftest.core").unlink()
        _commit_all(tmp_path)

        assert _scaffold_core_findings(tmp_path) == []


class TestGuidanceVenueNote:
    """Guidance that names Booley Flows must scope them to the Sandbox.

    The Project Root's CLAUDE.md link resolves on the host too, so an unscoped
    file tells a host-side agent to call MCP tools that do not exist there.
    """

    def _canon(self, tmp_path: Path, body: str) -> Path:
        if tmp_path.name != ".booley":
            tmp_path = tmp_path / ".booley"
            tmp_path.mkdir()
        canon = tmp_path / "AGENTS.md"
        canon.write_text(body, encoding="utf-8")
        return canon

    @pytest.mark.parametrize("location", ("inside the Sandbox", "Session Runtime"))
    def test_scoped_guidance_passes(self, tmp_path, location):
        canon = self._canon(
            tmp_path,
            f"- The MCP tools below exist only {location}.\n"
            "- At the start of a tab, call `booley_status`.\n",
        )
        rec = _Rec()
        _record_report(
            readiness.check_guidance(
                readiness.ProjectAudit(canon.parent.parent, canon.parent, {}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            warned=rec.w,
        )
        assert rec.kinds() == {"pass"}

    def test_unscoped_btool_guidance_warns(self, tmp_path):
        canon = self._canon(tmp_path, "- At the start of a tab, call `booley_status`.\n")
        rec = _Rec()
        _record_report(
            readiness.check_guidance(
                readiness.ProjectAudit(canon.parent.parent, canon.parent, {}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            warned=rec.w,
        )
        assert rec.kinds() == {"pass", "warn"}
        assert any("host-side agent session" in message for _, message in rec.events)

    def test_guidance_without_btools_is_silent(self, tmp_path):
        """Nothing to scope — a project may legitimately not mention them."""
        canon = self._canon(tmp_path, "# AGENTS.md\n\n- Project purpose: a UART.\n")
        rec = _Rec()
        _record_report(
            readiness.check_guidance(
                readiness.ProjectAudit(canon.parent.parent, canon.parent, {}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            warned=rec.w,
        )
        assert rec.kinds() == {"pass"}
        assert all("scopes Booley" not in message for _, message in rec.events)

    def test_shipped_template_satisfies_the_check(self, tmp_path):
        """The template doctor's fix hint points at must itself pass the check."""
        from booley.runtime.paths import skills_dir

        template = skills_dir() / "booley-setup" / "AGENTS_TEMPLATE.md"
        canon = self._canon(tmp_path, template.read_text(encoding="utf-8"))
        rec = _Rec()
        _record_report(
            readiness.check_guidance(
                readiness.ProjectAudit(canon.parent.parent, canon.parent, {}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            warned=rec.w,
        )
        assert rec.kinds() == {"pass"}

    def test_automatic_profile_reports_missing_links_without_creating_them(
        self, tmp_path, monkeypatch
    ):
        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir()
        self._canon(booley_dir, "# Project guidance\n")
        project = readiness.ProjectAudit(tmp_path, booley_dir, {}, {}, "sim")
        monkeypatch.setattr(
            readiness,
            "ensure_guidance_links",
            lambda *_a, **_kw: pytest.fail("read-only Doctor repaired links"),
        )
        rec = _Rec()

        _record_report(
            readiness.check_guidance(project, mode=readiness.ReadinessMode.INSPECT),
            passed=rec.p,
            warned=rec.w,
        )

        assert rec.kinds() == {"warn"}
        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_manual_profile_repairs_guidance_links(self, tmp_path, monkeypatch):
        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir()
        self._canon(booley_dir, "# Project guidance\n")
        project = readiness.ProjectAudit(tmp_path, booley_dir, {}, {}, "sim")
        calls = []
        monkeypatch.setattr(
            readiness,
            "ensure_guidance_links",
            lambda root, data: calls.append((root, data)),
        )
        rec = _Rec()

        _record_report(
            readiness.check_guidance(project, mode=readiness.ReadinessMode.RECONCILE),
            passed=rec.p,
            warned=rec.w,
        )

        assert calls == [(tmp_path, booley_dir)]
        assert any("root links ensured" in message for _kind, message in rec.events)


def test_project_audit_reports_a_missing_booley_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    loaded = readiness.load_project(tmp_path)
    assert loaded.booley_dir is None
    assert loaded.project is None
    assert any("Booley Directory not found" in f.message for f in loaded.report.findings)
