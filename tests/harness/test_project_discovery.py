"""Project data repositories must not become the active checkout."""

import subprocess

import pytest

from booley.harness.booley import find_project_root
from booley.ticket_board import ticket_baseline


def test_nested_data_repository_selects_checkout(tmp_path, monkeypatch):
    outer = tmp_path / "checkout"
    data = outer / ".booley_project"
    data.mkdir(parents=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.chdir(data)
    assert find_project_root() == outer


@pytest.mark.parametrize("sandbox", [False, True])
def test_standalone_board_root_and_io_use_owning_checkout(tmp_path, monkeypatch, sandbox):
    from booley.runtime.project_dir import reset_cache
    from booley.ticket_board.helpers import detect_project_root
    from booley.ticket_board.io import TicketIO

    outer = tmp_path / "checkout"
    data = tmp_path / "data" if sandbox else outer / ".booley_project"
    data.mkdir(parents=True)
    outer.mkdir(exist_ok=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    if sandbox:
        from booley.runtime import project_discovery

        (outer / "booley.toml").write_text(f'[project]\ndir = "{data.as_posix()}"\n')
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setattr(project_discovery, "WORK_DIR", str(outer))
        monkeypatch.setattr(project_discovery, "PROJECT_DIR_TARGET", str(data))
    for name in (
        "RTL_PROJECT_ROOT",
        "PROJECT_ROOT",
        "BOOLEY_CONTROL_PROJECT_ROOT",
        "BOOLEY_TICKET_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    reset_cache()
    monkeypatch.chdir(data)
    assert detect_project_root() == outer
    assert TicketIO(data / "tickets")._project_root == outer


@pytest.mark.parametrize("owning_cwd", [True, False])
def test_ticket_io_external_data_checks_current_checkout(tmp_path, monkeypatch, owning_cwd):
    from booley.runtime.project_dir import reset_cache
    from booley.runtime.project_discovery import ProjectRootDiscoveryError
    from booley.ticket_board.io import TicketIO

    outer, data, unrelated = (tmp_path / name for name in ("checkout", "data", "other"))
    for root in (outer, data, unrelated):
        root.mkdir()
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    (outer / "booley.toml").write_text(f"[project]\ndir = '{data.as_posix()}'\n")
    monkeypatch.chdir(outer if owning_cwd else unrelated)
    for name in ("RTL_PROJECT_ROOT", "PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    reset_cache()
    if owning_cwd:
        assert TicketIO(data / "tickets")._project_root == outer
    else:
        with pytest.raises(ProjectRootDiscoveryError, match="run from the Project checkout"):
            TicketIO(data / "tickets")


@pytest.mark.parametrize("config_name", ["booley.toml", "pyproject.toml"])
def test_ticket_io_invalid_utf8_checkout_config_refuses_cleanly(
    tmp_path, monkeypatch, config_name
):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError
    from booley.ticket_board.io import TicketIO

    outer, data = tmp_path / "checkout", tmp_path / "data"
    for root in (outer, data):
        root.mkdir()
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    (outer / config_name).write_bytes(b'[project]\ndir = "\xff"\n')
    monkeypatch.chdir(outer)
    for name in ("RTL_PROJECT_ROOT", "PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project checkout"):
        TicketIO(data / "tickets")


def test_ticket_io_nul_config_path_refuses_cleanly(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError
    from booley.ticket_board.io import TicketIO

    outer, data = tmp_path / "checkout", tmp_path / "data"
    for root in (outer, data):
        root.mkdir()
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    (outer / "booley.toml").write_text('[project]\ndir = "a\\u0000b"\n', encoding="utf-8")
    monkeypatch.chdir(outer)
    for name in ("RTL_PROJECT_ROOT", "PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project checkout"):
        TicketIO(data / "tickets")


@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_ticket_io_current_checkout_failure_is_specific(tmp_path, monkeypatch, failure_type):
    from booley.runtime import project_discovery
    from booley.ticket_board.io import TicketIO

    data = tmp_path / ".booley_project"
    data.mkdir()
    subprocess.run(["git", "init", str(data)], check=True, capture_output=True, timeout=30)
    for name in ("RTL_PROJECT_ROOT", "PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))

    def unavailable():
        raise failure_type("current checkout unavailable")

    expected = (
        project_discovery.ProjectRootDiscoveryError if failure_type is OSError else ValueError
    )
    with monkeypatch.context() as isolated:
        isolated.setattr(project_discovery.Path, "cwd", unavailable)
        with pytest.raises(expected):
            TicketIO(data / "tickets")


@pytest.mark.parametrize("error_kind", ["authored", "baseline", "ancestry", "discovery"])
def test_standalone_board_typed_errors_render_cleanly(tmp_path, monkeypatch, capsys, error_kind):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError
    from booley.ticket_board import cli
    from booley.ticket_board.io import TicketValidationError
    from booley.ticket_board.ticket_baseline import (
        TicketAncestryVerificationError,
        TicketBaselineError,
    )

    errors = {
        "authored": TicketValidationError,
        "baseline": TicketBaselineError,
        "ancestry": TicketAncestryVerificationError,
        "discovery": ProjectRootDiscoveryError,
    }
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: tmp_path / "tickets")
    monkeypatch.setattr(cli, "open_board", lambda *a, **kw: None)

    def fail(*args):
        raise errors[error_kind]("actionable failure")

    if error_kind == "discovery":
        monkeypatch.setattr(cli, "TicketIO", fail)
    else:
        monkeypatch.setitem(cli.HANDLERS, "show", fail)
    assert cli.main(["show", "ticket"]) == 2
    assert "actionable failure" in capsys.readouterr().err


def test_missing_object_is_operational_ancestry_failure(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    with pytest.raises(RuntimeError, match="cannot verify ancestry") as caught:
        ticket_baseline._descendant_commit(tmp_path, "a" * 40, "b" * 40, role="outer")
    assert "acceptance-input-change-required" not in str(caught.value)


@pytest.mark.parametrize("name", [".booley_project", ".booley/project", "custom-data"])
def test_all_checkout_local_data_selections(tmp_path, monkeypatch, name):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / name
    data.mkdir(parents=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    if name == "custom-data":
        (outer / "booley.toml").write_text('[project]\ndir = "custom-data"\n')
    child = data / "child"
    child.mkdir()
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(child) == outer


def test_unrelated_nested_checkout_keeps_identity(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    nested = tmp_path / "nested"
    nested.mkdir()
    for root in (tmp_path, nested):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(nested) == nested


@pytest.mark.parametrize("explicit", [False, True])
def test_ticket_design_worktree_inside_data_keeps_checkout(tmp_path, monkeypatch, explicit):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / ".booley_project"
    data.mkdir(parents=True)
    subprocess.run(["git", "init", str(outer)], check=True, capture_output=True, timeout=30)
    subprocess.run(
        [
            "git",
            "-C",
            str(outer),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    worktree = data / "worktrees/ticket"
    subprocess.run(
        ["git", "-C", str(outer), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    snapshot = worktree / ".booley_project"
    snapshot.mkdir()
    subprocess.run(["git", "init", str(snapshot)], check=True, capture_output=True, timeout=30)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    if explicit:
        monkeypatch.setenv("RTL_PROJECT_ROOT", str(worktree))
    else:
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    assert discover_project_root(worktree) == worktree
    assert discover_project_root(snapshot) == worktree
    if explicit:
        assert discover_project_root(data) == worktree


def test_detached_data_refuses_and_explicit_outer_disambiguates(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError, discover_project_root

    data = tmp_path / "data"
    outer = tmp_path / "checkout"
    data.mkdir()
    outer.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project checkout"):
        discover_project_root(data)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(data))
    with pytest.raises(ProjectRootDiscoveryError):
        discover_project_root(data)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(outer))
    assert discover_project_root(data) == outer


def test_empty_git_marker_and_fallback_priority(tmp_path, monkeypatch):
    from booley.runtime.shared_infra import resolve_project_root

    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    fallback = tmp_path / "fallback"
    assert resolve_project_root(fallback) == fallback
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(tmp_path / "explicit"))
    assert resolve_project_root(fallback) == tmp_path / "explicit"


@pytest.mark.parametrize("code", [0, 1, 128])
def test_baseline_ancestry_rc_matrix(tmp_path, monkeypatch, code):
    from booley.ticket_board.ticket_baseline import (
        TicketAncestryVerificationError,
        TicketBaselineError,
    )

    monkeypatch.setattr(
        ticket_baseline.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, code, "", "missing object"),
    )
    if code == 0:
        assert (
            ticket_baseline._descendant_commit(tmp_path, "child", "parent", role="outer")
            == "child"
        )
    else:
        error = TicketBaselineError if code == 1 else TicketAncestryVerificationError
        with pytest.raises(error) as caught:
            ticket_baseline._descendant_commit(tmp_path, "child", "parent", role="outer")
        assert ("acceptance-input-change-required" in str(caught.value)) == (code == 1)


def test_board_boundary_does_not_mask_programmer_valueerror(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from booley.harness import booley
    from booley.ticket_board.io import TicketValidationError

    def invalid(*args):
        raise TicketValidationError("invalid authored Ticket")

    monkeypatch.setattr(booley, "_run_board_command", invalid)
    assert booley._cmd_board(Namespace(), tmp_path) == 2
    assert "invalid authored Ticket" in capsys.readouterr().err

    def programmer(*args):
        raise ValueError("programmer failure")

    monkeypatch.setattr(booley, "_run_board_command", programmer)
    with pytest.raises(ValueError, match="programmer failure"):
        booley._cmd_board(Namespace(), tmp_path)


@pytest.mark.parametrize("owned", [True, False])
def test_sandbox_requires_checkout_local_ownership(tmp_path, monkeypatch, owned):
    from booley.runtime import project_discovery as discovery

    outer = tmp_path / "checkout"
    data = tmp_path / "data"
    outer.mkdir()
    data.mkdir()
    subprocess.run(["git", "init", str(outer)], check=True, capture_output=True, timeout=30)
    if owned:
        (outer / "booley.toml").write_text(f'[project]\ndir = "{data.as_posix()}"\n')
    monkeypatch.setattr(discovery, "WORK_DIR", str(outer))
    monkeypatch.setattr(discovery, "PROJECT_DIR_TARGET", str(data))
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    if owned:
        assert discovery.discover_project_root(data) == outer
    else:
        with pytest.raises(discovery.ProjectRootDiscoveryError):
            discovery.discover_project_root(data)


def test_linked_git_marker_and_symlink_data(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / ".booley_project"
    data.mkdir(parents=True)
    (outer / ".git").write_text("gitdir: /unused/metadata\n")
    alias = tmp_path / "alias"
    alias.symlink_to(data, target_is_directory=True)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(alias) == outer


def test_source_stale_data_refuses_board_but_feedback_routes(tmp_path, monkeypatch):
    from booley.harness.booley import _command_project_root
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    data = tmp_path / ".booley_project"
    data.mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    monkeypatch.chdir(data)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    with pytest.raises(ProjectRootDiscoveryError):
        _command_project_root("board")
    assert _command_project_root("feedback") == tmp_path


def test_promotion_git_failure_does_not_block_or_publish(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from booley.ticket_board import basis_refresh, operations
    from booley.ticket_board.ticket_baseline import TicketAncestryVerificationError

    state = {"failed": False, "operation": ""}
    updates = {}

    def fail(*args):
        raise TicketAncestryVerificationError("cannot verify ancestry: missing object")

    monkeypatch.setattr(basis_refresh, "prepare_waiting_basis_refresh", fail)
    tio = SimpleNamespace(_project_root=tmp_path, tickets_dir=tmp_path)
    ticket = {"machine": {}, "file": "ticket.md"}
    assert not operations._refresh_waiting_basis(tio, ticket, "ticket", updates, state)
    assert state == {"failed": False, "operation": ""}
    assert updates == {}
    assert "acceptance-input-change-required" not in capsys.readouterr().err


@pytest.mark.parametrize("use_start", [False, True])
def test_checkout_owned_external_symlink_data_resolves_logical_parent(
    tmp_path, monkeypatch, use_start
):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = tmp_path / "external-data"
    outer.mkdir()
    data.mkdir()
    (outer / ".booley_project").symlink_to(data, target_is_directory=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.chdir(data)
    monkeypatch.setenv("PWD", str(outer / ".booley_project"))
    assert discover_project_root(outer / ".booley_project" if use_start else None) == outer


@pytest.mark.parametrize("config", ['project = "invalid"', "[project]\ndir = 12"])
def test_malformed_project_selection_does_not_crash_discovery(tmp_path, monkeypatch, config):
    from booley.runtime.project_discovery import discover_project_root

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    (tmp_path / "booley.toml").write_text(config)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(tmp_path) == tmp_path


@pytest.mark.parametrize("failure_kind", ["timeout", "oserror"])
def test_baseline_transport_failure_is_typed_operational_error(
    tmp_path, monkeypatch, failure_kind
):
    from booley.ticket_board.ticket_baseline import TicketAncestryVerificationError

    failure = (
        subprocess.TimeoutExpired("git", 30)
        if failure_kind == "timeout"
        else OSError("git unavailable")
    )

    def fail(*a, **kw):
        raise failure

    monkeypatch.setattr(ticket_baseline.subprocess, "run", fail)
    with pytest.raises(TicketAncestryVerificationError, match="cannot verify ancestry") as caught:
        ticket_baseline._descendant_commit(tmp_path, "child", "parent", role="outer")
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("failure_kind", ["missing", "timeout", "oserror"])
def test_baseline_ref_identity_failure_is_operational(tmp_path, monkeypatch, failure_kind):
    from booley.ticket_board.ticket_baseline import TicketAncestryVerificationError

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    if failure_kind != "missing":
        failure = (
            subprocess.TimeoutExpired("git", 30)
            if failure_kind == "timeout"
            else OSError("git unavailable")
        )

        def fail(*a, **kw):
            raise failure

        monkeypatch.setattr(ticket_baseline.subprocess, "run", fail)
    with pytest.raises(TicketAncestryVerificationError, match="cannot verify ancestry") as caught:
        ticket_baseline._descendant_ref_commit(
            tmp_path, "missing-ref", "a" * 40, kind="ticket", role="outer"
        )
    assert "acceptance-input-change-required" not in str(caught.value)


def test_explicit_external_data_symlink_is_refused(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError, discover_project_root

    outer = tmp_path / "checkout"
    data = tmp_path / "external-data"
    outer.mkdir()
    data.mkdir()
    alias = outer / ".booley_project"
    alias.symlink_to(data, target_is_directory=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(alias))
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project checkout"):
        discover_project_root(outer)


def test_containment_alias_explicit_authority(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.runtime import project_discovery

    root, alias, unrelated = (tmp_path / name for name in ("selected", "alias", "unrelated"))
    for directory in (root, alias, unrelated):
        directory.mkdir()
    original = Path.samefile
    calls = []

    def samefile(path, other):
        if {path, Path(other)} == {root, alias}:
            calls.append(path)
            return True
        return original(path, other)

    monkeypatch.setattr(Path, "samefile", samefile)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root))
    assert project_discovery._data_root(alias / "new", []) == root
    assert calls
    nested = alias / "embedded"
    nested.mkdir()
    assert project_discovery._data_root(nested, [nested]) is None
    (nested / ".git").mkdir()
    (nested / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (nested / "booley.toml").write_text(f'[project]\ndir = "{root}"\n')
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / ".git").mkdir()
    (owner / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (owner / "booley.toml").write_text(f'[project]\ndir = "{root}"\n')
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(unrelated))
    from booley.runtime.project_dir import reset_cache, resolve_project_dir

    reset_cache()
    assert resolve_project_dir() == unrelated
    assert project_discovery._data_root(alias / "new", [owner]) == root
    assert project_discovery._owning_checkouts([owner], root) == {owner}
    reset_cache()
    assert project_discovery._owning_checkouts([nested], root) == set()
