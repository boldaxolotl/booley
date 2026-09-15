"""Migrated readiness diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness.setup import readiness
from booley.runtime.project_dir import reset_cache, resolve_project_dir
from tests.diagnostic_helpers import (
    _Rec,
    _record_report,
)
from tests.harness.test_doctor import _write_project


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
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(canonical_dir))
    assert resolve_project_dir() == canonical_dir.resolve()  # pre-warm session-global cache

    audit = readiness.load_project(checkout_root).project

    assert audit is not None
    assert audit.project_dir == checkout_dir.resolve()
    assert audit.booley_toml["project"]["name"] == "ticket"


class TestStealthCoresCheck:
    """ADR 0036 contract: authored cores live in .booley_project/cores/ (and
    nowhere else in the state dir), and never share a VLNV with a repo core."""

    def _project(self, tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "repo"
        project_dir = root / ".booley_project"
        (project_dir / "cores").mkdir(parents=True)
        return root, project_dir

    def test_clean_layout_passes(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (root / "repo.core").write_text("CAPI=2:\nname: ::repo:0\n", encoding="utf-8")
        (project_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, project_dir, {"stealth": {"enabled": True}}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert rec.kinds() == {"pass"}

    def test_stranded_core_fails_with_move_hint(self, tmp_path: Path):
        # The original stealth-project layout: authored cores directly under
        # .booley_project/ — structurally skipped, so Targets silently vanish.
        root, project_dir = self._project(tmp_path)
        (project_dir / "stranded.core").write_text("CAPI=2:\nname: ::s:0\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, project_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert "stranded" in rec.fails()[0]
        assert "stranded.core" in rec.fails()[0]

    def test_worktree_copies_are_not_stranded(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        wt = project_dir / "worktrees" / "t1"
        wt.mkdir(parents=True)
        (wt / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        bl = project_dir / ".baseline-wt-7-abc"
        bl.mkdir()
        (bl / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, project_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert rec.kinds() == {"pass"}

    def test_private_registry_copies_are_not_stranded(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        registry = project_dir / "tmp" / "fusesoc-isolated-cores"
        registry.mkdir(parents=True)
        (registry / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        rec = _Rec()

        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, project_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )

        assert rec.kinds() == {"pass"}

    def test_cross_root_collision_fails(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (root / "repo.core").write_text("CAPI=2:\nname: ::dup:0\n", encoding="utf-8")
        (project_dir / "cores" / "s.core").write_text("CAPI=2:\nname: ::dup:1\n", encoding="utf-8")
        rec = _Rec()
        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, project_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )
        assert any("both core roots" in msg for msg in rec.fails())

    def test_hidden_cores_require_stealth_mode(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (project_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        rec = _Rec()

        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, project_dir, {"stealth": {"enabled": False}}, {}, ""),
                mode=readiness.ReadinessMode.INSPECT,
            ),
            passed=rec.p,
            failed=rec.f,
        )

        assert any("stealth mode is disabled" in msg for msg in rec.fails())

    def test_stealth_projection_is_repaired(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        (project_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        rec = _Rec()

        _record_report(
            readiness.check_stealth_cores(
                readiness.ProjectAudit(root, project_dir, {"stealth": {"enabled": True}}, {}, ""),
                mode=readiness.ReadinessMode.RECONCILE,
            ),
            passed=rec.p,
            failed=rec.f,
        )

        assert rec.fails() == []
        assert (root / ".booley-projected-s.core").is_file()


class TestGuidanceVenueNote:
    """Guidance that names Booley Flows must scope them to the Session Runtime.

    The repo root's CLAUDE.md link resolves on the host too, so an unscoped
    file tells a host-side agent to call MCP tools that do not exist there.
    """

    def _canon(self, tmp_path: Path, body: str) -> Path:
        if tmp_path.name != ".booley_project":
            tmp_path = tmp_path / ".booley_project"
            tmp_path.mkdir()
        canon = tmp_path / "AGENTS.md"
        canon.write_text(body, encoding="utf-8")
        return canon

    def test_scoped_guidance_passes(self, tmp_path):
        canon = self._canon(
            tmp_path,
            "- The MCP tools below exist only inside the Session Runtime.\n"
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
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        self._canon(project_dir, "# Project guidance\n")
        project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
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
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        self._canon(project_dir, "# Project guidance\n")
        project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
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

        assert calls == [(tmp_path, project_dir)]
        assert any("root links ensured" in message for _kind, message in rec.events)


def test_project_audit_reports_a_missing_project_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    loaded = readiness.load_project(tmp_path)
    assert loaded.project_dir is None
    assert loaded.project is None
    assert any("project directory not found" in f.message for f in loaded.report.findings)
