"""Directory authority refusals retain accurate ownership and recovery diagnostics."""

from __future__ import annotations

import os
from unittest.mock import Mock

import pytest

from booley.audit.diagnostic_results import Findings
from booley.core.booley_dir import checkout_booley_dir_relative_to, reset_cache
from booley.eda.provisioning.authority import AuthorityError, _canonical_project
from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows import baseline_worktree as baseline
from booley.harness import doctor, init_cmd
from booley.harness.setup import cleanup, docker_image, guidance_links, readiness
from booley.harness.setup.common import InitContext
from booley.projects import inventory
from booley.runtime import project_worktree_pairing
from tests.diagnostic_helpers import _Rec
from tests.flows.test_baseline_worktree import _init_repo, _paired_checkout_without_upstream
from tests.test_guidance_links import _make_project


@pytest.fixture(autouse=True)
def _isolate_project_cache():
    reset_cache()
    yield
    reset_cache()


def test_checkout_cannot_be_its_own_booley_directory(tmp_path, monkeypatch):
    config = tmp_path / "booley.toml"
    original = '[project]\ndir = "."\n'
    config.write_text(original, encoding="utf-8")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    with pytest.raises(ValueError, match="Booley Directory cannot be the checkout root"):
        checkout_booley_dir_relative_to(tmp_path)
    assert config.read_text(encoding="utf-8") == original


def test_authority_refuses_unavailable_project_root(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(AuthorityError, match="Project Root is unavailable") as failure:
        _canonical_project(missing)
    assert str(missing) in str(failure.value)
    assert not missing.exists()


def test_paired_baseline_requires_a_commit():
    with pytest.raises(ValueError, match="Paired Checkout baseline requires a commit"):
        PairedProjectBaseline.entry_pinned("")


def test_missing_paired_baseline_preserves_source_and_cleans_scratch(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    reset_cache()
    head = baseline.git_full_sha("HEAD", tmp_path)
    before = baseline._git(tmp_path, "worktree", "list", "--porcelain", timeout=10).stdout
    with (
        pytest.raises(baseline.BaselineWorktreeError, match="Paired Checkout is unavailable"),
        baseline.baseline_worktree(
            tmp_path, "HEAD", paired_project=PairedProjectBaseline.entry_pinned(head)
        ),
    ):
        pytest.fail("missing paired inputs were accepted")
    assert baseline.git_full_sha("HEAD", tmp_path) == head
    assert baseline._git(tmp_path, "worktree", "list", "--porcelain", timeout=10).stdout == before


def test_absent_pairing_policy_refuses_existing_paired_inputs(tmp_path, monkeypatch):
    outer, paired, _base = _paired_checkout_without_upstream(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(paired))
    reset_cache()
    head = baseline.git_full_sha("HEAD", paired)
    with (
        pytest.raises(baseline.BaselineWorktreeError, match="unexpected Paired Checkout"),
        baseline.baseline_worktree(outer, "HEAD", paired_project=PairedProjectBaseline.absent()),
    ):
        pytest.fail("unexpected paired inputs were accepted")
    assert baseline.git_full_sha("HEAD", paired) == head


def test_missing_pairing_on_cleanup_preserves_unowned_directory(tmp_path, caplog):
    _init_repo(tmp_path)
    scratch = tmp_path / "unowned-baseline"
    scratch.mkdir()
    sentinel = scratch / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    baseline._remove_paired_project_baseline(tmp_path, scratch)
    assert "Paired Checkout disappeared before baseline cleanup" in caplog.text
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_doctor_outside_git_names_the_rtl_repository(tmp_path):
    rec = _Rec()
    doctor._check_git_state(tmp_path, rec.p, rec.n, rec.f)
    assert ("fail", "not inside a git work tree") in rec.events
    assert any("RTL repository" in fix for fix in rec.fix_hints)
    assert not any(level == "pass" for level, _message in rec.events)


def test_missing_booley_directory_prevents_agent_selection_write(tmp_path, capsys):
    ctx = InitContext(project_root=tmp_path)
    config = tmp_path / ".booley" / "booley.toml"
    selection = init_cmd.AgentSelection("claude", "api_key", True, True)
    assert not init_cmd._step_agent_config(ctx, selection, config)
    assert ctx.results[-1].name == "agent_config"
    assert ctx.results[-1].status == "err"
    assert "Booley Directory is missing" in capsys.readouterr().out
    assert not config.parent.exists()


def test_inventory_failure_retains_actionable_retry(tmp_path, monkeypatch, capsys):
    ctx = InitContext(project_root=tmp_path)
    monkeypatch.setattr(
        init_cmd, "remember_project", Mock(side_effect=inventory.ProjectInventoryError("denied"))
    )
    init_cmd._step_project_inventory(ctx)
    assert ctx.results[-1].status == "err"
    assert "booley projects discover" in ctx.results[-1].detail
    assert "could not remember Project Root" in capsys.readouterr().out


def test_interactive_init_banner_names_selected_project_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(init_cmd.sys.stdout, "isatty", lambda: True)
    init_cmd._print_init_banner(InitContext(project_root=tmp_path))
    output = capsys.readouterr().out
    assert "Project Root" in output and str(tmp_path) in output


def test_cleanup_resolution_failure_is_typed_and_preserves_files(tmp_path, monkeypatch):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        cleanup, "resolve_checkout_booley_dir", Mock(side_effect=PermissionError("denied"))
    )
    with pytest.raises(cleanup.CleanupError, match="cannot resolve the Booley Directory"):
        cleanup._project_root(tmp_path)
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name == "nt", reason="directory symlink requires host privilege")
def test_cleanup_refuses_a_substituted_directory_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / ".booley").symlink_to(outside, target_is_directory=True)
    with pytest.raises(cleanup.CleanupError, match="Booley Directory must not be a symlink"):
        cleanup._safe_relative(root, ".booley/report.json")
    assert outside.is_dir() and not list(outside.iterdir())


def test_local_image_build_reports_missing_source_checkout(tmp_path, capsys):
    docker_dir = tmp_path / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    for name in ("Dockerfile", "Dockerfile.base"):
        (docker_dir / name).write_text("FROM example", encoding="utf-8")
    ctx = InitContext(project_root=tmp_path)
    assert docker_image._local_build_inputs(ctx, docker_dir) is None
    assert ctx.results[-1].detail == "Booley repository root not found"
    output = capsys.readouterr()
    assert "cannot determine Booley repository root" in output.out
    assert "build manually" in output.out


def test_guidance_rejects_another_project_roots_plan_before_writing(tmp_path):
    first, directory, _canon = _make_project(tmp_path / "first")
    second, second_directory, second_canon = _make_project(tmp_path / "second")
    plan = guidance_links.plan_guidance_links(first, directory)
    original = second_canon.read_bytes()
    with pytest.raises(ValueError, match="different Project Root"):
        guidance_links.ensure_guidance_links(second, second_directory, plan=plan)
    assert second_canon.read_bytes() == original
    assert not (second / "AGENTS.md").exists()


def test_configuration_validation_reports_disappeared_directory(tmp_path):
    missing = tmp_path / ".booley"
    findings = Findings()
    assert not readiness._validate_booley_toml({}, missing, findings)
    failures = findings.report().findings
    assert any(
        "Booley Directory missing" in finding.message and finding.fix == "booley init"
        for finding in failures
    )


def test_forget_unremembered_project_preserves_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir()
    before = inventory.project_inventory()
    with pytest.raises(inventory.ProjectInventoryError, match="Project Root is not remembered"):
        inventory.forget_project(project)
    assert inventory.project_inventory() == before


def test_pairing_refuses_to_replace_an_existing_git_checkout(tmp_path):
    nested = tmp_path / ".booley"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: keep", encoding="utf-8")
    with pytest.raises(
        project_worktree_pairing.ProjectPairingError, match="Booley Directory with a Git checkout"
    ):
        project_worktree_pairing._remove_copy(tmp_path)
    assert (nested / ".git").read_text(encoding="utf-8") == "gitdir: keep"


@pytest.mark.skipif(os.name == "nt", reason="directory symlink requires host privilege")
def test_pairing_refuses_an_escaping_directory_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".booley").symlink_to(outside, target_is_directory=True)
    with pytest.raises(
        project_worktree_pairing.ProjectPairingError, match="escapes its containing worktree"
    ):
        project_worktree_pairing._remove_copy(outer)
    assert outside.is_dir()
