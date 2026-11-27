"""Directory authority refusals retain accurate ownership and recovery diagnostics."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from booley.audit.diagnostic_results import Findings
from booley.core.booley_dir import checkout_booley_dir_relative_to, reset_cache
from booley.core.host_shell import quote_arg
from booley.eda.provisioning.authority import AuthorityError, grant_project_identity
from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows import baseline_worktree as baseline
from booley.harness import init_cmd
from booley.harness.setup import docker_image, guidance_links, readiness
from booley.harness.setup.common import InitContext
from booley.projects import inventory
from booley.runtime import project_worktree_pairing
from tests.booley_checkouts_support import commit_all, init_repo, isolate_git
from tests.flows.test_baseline_worktree import _init_repo, _paired_checkout_without_upstream
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.harness.doctor_host_support import answer_doctor_commands
from tests.harness.doctor_run_support import run_doctor, write_project
from tests.harness.init_agent_support import empty_host, seed_init, stub_host_images
from tests.project_runtime_git_support import git
from tests.test_guidance_links import _make_project


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
        grant_project_identity(missing)
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
    before = git(tmp_path, "worktree", "list", "--porcelain")
    with (
        pytest.raises(baseline.BaselineWorktreeError, match="Paired Checkout is unavailable"),
        baseline.baseline_worktree(
            tmp_path, "HEAD", paired_project=PairedProjectBaseline.entry_pinned(head)
        ),
    ):
        pytest.fail("missing paired inputs were accepted")
    assert baseline.git_full_sha("HEAD", tmp_path) == head
    assert git(tmp_path, "worktree", "list", "--porcelain") == before


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
    # private: baseline_worktree removes the whole outer worktree right after this
    # step, so only the step shows that it leaves a directory it does not own alone.
    baseline._remove_paired_project_baseline(tmp_path, scratch)
    assert "Paired Checkout disappeared before baseline cleanup" in caplog.text
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_doctor_outside_git_names_the_rtl_repository(tmp_path, monkeypatch):
    patch_doctor_environment(monkeypatch, tmp_path, write_project(tmp_path))
    outside_git = subprocess.CompletedProcess([], 128, "", "fatal: not a git repository")
    answer_doctor_commands(
        monkeypatch,
        lambda argv, _env: (
            outside_git if argv[1:3] == ["rev-parse", "--is-inside-work-tree"] else None
        ),
    )

    findings = run_doctor(tmp_path).findings

    git_rows = [(f.severity.value, f.message, f.fix) for f in findings if "git" in f.message]
    assert ("fail", "not inside a git work tree", "run doctor from the RTL repository") in git_rows
    assert "git repository detected" not in [message for _s, message, _f in git_rows]


def test_missing_booley_directory_prevents_agent_selection_write(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    empty_host(tmp_path, monkeypatch)
    stub_host_images(monkeypatch)

    assert seed_init(root, provider="claude") == 2

    out = capsys.readouterr().out
    assert "cannot persist agent selection: Booley Directory is missing" in out
    assert "agent_config — Booley Directory missing" in out
    assert not (root / ".booley").exists()


def test_inventory_failure_retains_actionable_retry(tmp_path, capsys):
    root = tmp_path / "it's a root"  # not initialized, so the inventory refuses it
    root.mkdir()
    ctx = InitContext(project_root=root)
    # private: `booley init` runs this step only after every earlier step succeeded,
    # including Sandbox Issuance, which a test host passes only by patching that step.
    init_cmd._step_project_inventory(ctx)
    assert ctx.results[-1].status == "err"
    assert f"booley projects discover {quote_arg(str(root))}" in ctx.results[-1].detail
    assert "could not remember Project Root" in capsys.readouterr().out


def test_interactive_init_banner_names_selected_project_root(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    empty_host(tmp_path, monkeypatch)
    stub_host_images(monkeypatch)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    seed_init(root)

    assert f"Project Root  : {root}" in capsys.readouterr().out


def test_local_image_build_reports_missing_source_checkout(tmp_path, capsys):
    docker_dir = tmp_path / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    for name in ("Dockerfile", "Dockerfile.base"):
        (docker_dir / name).write_text("FROM example", encoding="utf-8")
    ctx = InitContext(project_root=tmp_path)
    # private: the image step finds the Dockerfiles inside the installed package; no
    # public entry point can point it at a directory outside a source checkout.
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
    # private: Doctor reads booley.toml from this directory first, so only a directory
    # removed mid-run reaches this refusal.
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


@pytest.fixture
def versioned_source(tmp_path, monkeypatch):
    """A committed Versioned Booley Directory to pair a new worktree with."""
    isolate_git(tmp_path, monkeypatch)
    source = tmp_path / "versioned"
    init_repo(source)
    (source / "booley.toml").write_text("", encoding="utf-8")
    commit_all(source)
    return source


def _pair(tmp_path, outer, source):
    with pytest.raises(project_worktree_pairing.ProjectPairingError) as refused:
        project_worktree_pairing.pair_project_worktree(tmp_path, outer, "p1", source=source)
    assert "booley-worktree/p1" not in git(source, "branch", "--list")
    return str(refused.value)


def test_pairing_refuses_to_replace_an_existing_git_checkout(tmp_path, versioned_source):
    nested = tmp_path / "outer" / ".booley"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: keep", encoding="utf-8")
    assert "Booley Directory with a Git checkout" in _pair(
        tmp_path, nested.parent, versioned_source
    )
    assert (nested / ".git").read_text(encoding="utf-8") == "gitdir: keep"


@pytest.mark.skipif(os.name == "nt", reason="directory symlink requires host privilege")
def test_pairing_refuses_an_escaping_directory_symlink(tmp_path, versioned_source):
    outside = tmp_path / "outside"
    outside.mkdir()
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".booley").symlink_to(outside, target_is_directory=True)
    assert "escapes its containing worktree" in _pair(tmp_path, outer, versioned_source)
    assert outside.is_dir()
