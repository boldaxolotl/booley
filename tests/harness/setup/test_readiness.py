"""Migrated readiness diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.fusesoc import core_projection
from booley.harness.setup import readiness
from booley.harness.setup.guidance_links import ensure_guidance_links
from booley.harness.setup.project_origin import record_scaffold_origin
from booley.harness.setup.scaffold import ScaffoldChoices, scaffold_files
from booley.runtime.booley_dir import reset_cache, resolve_booley_dir
from booley.runtime.paths import skills_dir
from tests.git_command_fake import GitCommandFake
from tests.harness.test_doctor import _write_project
from tests.subprocess_budget import run_bounded_child


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


def test_doctor_names_enabled_flags_without_changing_its_verdict(
    tmp_path, monkeypatch, feature_flags
):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    plain_root = tmp_path / "plain"
    flagged_root = tmp_path / "flagged"
    plain_root.mkdir()
    flagged_root.mkdir()
    _write_project(plain_root)
    feature_flags.enable(_write_project(flagged_root), "test-flag")

    plain = readiness.load_project(plain_root).report
    flagged = readiness.load_project(flagged_root).report

    assert flagged.health == plain.health
    assert not any("test-flag" in f.message for f in plain.findings)
    named = [f for f in flagged.findings if "test-flag" in f.message]
    assert [f.severity for f in named] == ["pass"]


def _core(name: str) -> str:
    return f"CAPI=2:\nname: ::{name}:0\n"


@pytest.mark.parametrize(
    ("cores", "stealth", "failure"),
    [
        pytest.param(
            {"repo.core": "repo", ".booley/cores/s.core": "stealth"}, True, None, id="clean"
        ),
        # The original stealth layout: cores directly under .booley/ are skipped, so their
        # Targets silently vanish.
        pytest.param({".booley/stranded.core": "s"}, False, "stranded.core", id="stranded"),
        pytest.param(
            {
                ".booley/worktrees/t1/copy.core": "c",
                ".booley/.baseline-wt-7-abc/copy.core": "c",
                ".booley/tmp/fusesoc-isolated-cores/copy.core": "c",
            },
            False,
            None,
            id="worktree-and-registry-copies",
        ),
        pytest.param(
            {"repo.core": "dup", ".booley/cores/s.core": "dup"},
            False,
            "both core roots",
            id="cross-root-collision",
        ),
        pytest.param(
            {".booley/cores/s.core": "stealth"},
            False,
            "stealth mode is disabled",
            id="hidden-without-stealth",
        ),
    ],
)
def test_stealth_cores_live_only_in_booley_cores(
    tmp_path: Path, cores: dict[str, str], stealth: bool, failure: str | None
):
    """ADR 0036: authored cores live in .booley/cores/ (and nowhere else in the Booley
    Directory), and never share a VLNV with a repo core."""
    root = tmp_path / "repo"
    (root / ".booley" / "cores").mkdir(parents=True)
    for rel, name in cores.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(_core(name), encoding="utf-8")
    if stealth:
        (root / ".booley/booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        core_projection.reconcile_projected_cores(root)
    audit = readiness.ProjectAudit(
        root, root / ".booley", {"stealth": {"enabled": stealth}}, {}, ""
    )

    report = readiness.check_stealth_cores(audit)

    if failure is None:
        assert all(f.severity is Severity.PASS for f in report.findings), report.findings
    else:
        failures = [f.message for f in report.findings if f.severity is Severity.FAIL]
        assert any(failure in message for message in failures), report.findings


_SCAFFOLD_CHECK = "stealth.scaffold-core-tracked"
_SCAFFOLD_TEXT = "CAPI=2:\nname: ::my_ip:0\ndescription: {}\n"
_SCAFFOLD_DESCRIPTION = "my_ip starter IP (scaffolded by booley init --scaffold)"
_SELFTEST_CORE = "verif/booley_doctor_selftest.core"


def _commit_all(root: Path) -> None:
    for args in (["init", "-q"], ["add", "-f", "."], ["commit", "-qm", "scaffold"]):
        run_bounded_child(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=root,
            check=True,
        )


def _scaffold_core_findings(root: Path):
    audit = readiness.ProjectAudit(root, root / ".booley", {"stealth": {"enabled": True}}, "")
    report = readiness.check_stealth_cores(audit)
    return [f for f in report.findings if f.check_id == _SCAFFOLD_CHECK]


def _write_scaffold(root: Path) -> None:
    choices = ScaffoldChoices("my_ip", "verilator", "sv", "verilator", False, None)
    for rel, content in scaffold_files(choices).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    record_scaffold_origin(root / ".booley/booley.toml")


def test_tracked_scaffold_cores_warn_in_stealth(tmp_path: Path, monkeypatch):
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

    assert {f.subject for f in findings} == {"my_ip.core", _SELFTEST_CORE}
    assert all(".booley/cores/" in f.fix for f in findings)
    assert all(f.subject in f.message for f in findings)


def _native_core(root: Path, _scaffolded: str) -> None:
    (root / "ip.core").write_text(_SCAFFOLD_TEXT.format("A hand-written IP"), encoding="utf-8")


def _port_origin(root: Path, _scaffolded: str) -> None:
    toml = root / ".booley/booley.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "# booley-init-origin: scaffold", "# booley-init-origin: port"
        ),
        encoding="utf-8",
    )
    (root / "my_ip.core").write_text(
        _SCAFFOLD_TEXT.format(_SCAFFOLD_DESCRIPTION), encoding="utf-8"
    )


def _projection(root: Path, _scaffolded: str) -> None:
    (root / ".booley-projected-my_ip.core").write_text(
        _SCAFFOLD_TEXT.format(_SCAFFOLD_DESCRIPTION), encoding="utf-8"
    )


def _unreadable_core(root: Path, _scaffolded: str) -> None:
    (root / "my_ip.core").write_text("CAPI=2:\nname: [unclosed\n", encoding="utf-8")


def _relocated_core(root: Path, scaffolded: str) -> None:
    (root / ".booley/cores").mkdir(parents=True)
    (root / ".booley/cores/my_ip.core").write_text(scaffolded, encoding="utf-8")


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(_native_core, id="native-core"),
        pytest.param(_port_origin, id="port-origin"),
        pytest.param(_projection, id="projection"),
        # A tracked core Booley cannot parse is the core audit's finding, not this one.
        pytest.param(_unreadable_core, id="unreadable-core"),
        pytest.param(_relocated_core, id="relocated-stealth-core"),
    ],
)
def test_scaffold_core_warning_skips_cores_that_are_not_tracked_scaffold_cores(
    tmp_path: Path, edit
):
    _write_scaffold(tmp_path)
    scaffolded = (tmp_path / "my_ip.core").read_text(encoding="utf-8")
    for rel in ("my_ip.core", _SELFTEST_CORE):
        (tmp_path / rel).unlink()
    edit(tmp_path, scaffolded)
    _commit_all(tmp_path)

    assert _scaffold_core_findings(tmp_path) == []


@pytest.mark.parametrize("git", ["no-repository", "no-git-executable"])
def test_scaffold_core_warning_needs_git_to_say_what_is_tracked(
    tmp_path: Path, monkeypatch, git: str
):
    _write_scaffold(tmp_path)
    if git == "no-git-executable":
        _commit_all(tmp_path)
        GitCommandFake.install(monkeypatch).crash("ls-files", error=FileNotFoundError("git"))

    assert _scaffold_core_findings(tmp_path) == []


_SCOPED = "- The MCP tools below exist only {}.\n- At the start of a tab, call `booley_status`.\n"


@pytest.mark.parametrize(
    ("body", "links", "warnings"),
    [
        pytest.param(_SCOPED.format("inside the Sandbox"), True, [], id="scoped-sandbox"),
        pytest.param(_SCOPED.format("Session Runtime"), True, [], id="scoped-runtime"),
        # The root CLAUDE.md link resolves on the host too, so unscoped guidance tells a
        # host-side agent to call MCP tools that do not exist there.
        pytest.param(
            "- At the start of a tab, call `booley_status`.\n",
            True,
            ["guidance.session-runtime-scope"],
            id="unscoped-tools",
        ),
        pytest.param("# AGENTS.md\n\n- Project purpose: a UART.\n", True, [], id="no-tools"),
        # The template Doctor's fix hint points at must itself pass.
        pytest.param(None, True, [], id="shipped-template"),
        pytest.param("# Project guidance\n", False, ["guidance.links-unhealthy"], id="no-links"),
    ],
)
def test_guidance_must_scope_booley_tools_to_the_sandbox(
    tmp_path: Path, body: str | None, links: bool, warnings: list[str]
):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    template = skills_dir() / "booley-setup" / "AGENTS_TEMPLATE.md"
    text = template.read_text(encoding="utf-8") if body is None else body
    (booley_dir / "AGENTS.md").write_text(text, encoding="utf-8")
    if links:
        ensure_guidance_links(tmp_path, booley_dir)

    report = readiness.check_guidance(readiness.ProjectAudit(tmp_path, booley_dir, {}, ""))

    assert [f.check_id for f in report.findings if f.severity is not Severity.PASS] == warnings
    assert all(f.severity in (Severity.PASS, Severity.WARN) for f in report.findings)
    assert any("root links current" in f.message for f in report.findings) is links
    # Doctor only reports broken links; `booley init` owns the repair.
    assert all((tmp_path / name).exists() is links for name in ("AGENTS.md", "CLAUDE.md"))
    unhealthy = [f for f in report.findings if f.check_id == "guidance.links-unhealthy"]
    assert all("booley init" in f.fix for f in unhealthy)


def test_project_audit_reports_a_missing_booley_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    loaded = readiness.load_project(tmp_path)
    assert loaded.booley_dir is None
    assert loaded.project is None
    assert any("Booley Directory not found" in f.message for f in loaded.report.findings)
