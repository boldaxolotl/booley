"""No-agent ticket readiness checks exercise preparation and Target binding."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.fusesoc import fusesoc_registry
from booley.runtime.project_dir import reset_cache
from booley.ticket_board import readiness as readiness_module
from booley.ticket_board.io import TicketFileSpec, TicketIO
from booley.ticket_board.readiness import check_ticket_ready


@pytest.fixture(autouse=True)
def _clear_project_dir_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _remove_ticket_worktree(root: Path) -> Path:
    paths = [
        Path(line.removeprefix("worktree "))
        for line in _git(root, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    [workspace] = [path for path in paths if path.resolve() != root.resolve()]
    _git(root, "worktree", "remove", "--force", str(workspace))
    return workspace


def test_check_ticket_ready_prepares_generated_target_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text("/.booley_project\n/generated.hex\n", encoding="utf-8")
    (root / "rtl").mkdir()
    (root / "rtl" / "toy.sv").write_text("module toy; endmodule\n", encoding="utf-8")
    (root / "tb").mkdir()
    (root / "tb" / "toy_tb.sv").write_text(
        "module toy_tb; toy dut(); endmodule\n", encoding="utf-8"
    )
    (root / "toy.core").write_text(
        """CAPI=2:
name: acme:lib:toy:1.0
filesets:
  rtl:
    files: [rtl/toy.sv]
    file_type: systemVerilogSource
  tb:
    files: [tb/toy_tb.sv]
    file_type: systemVerilogSource
  firmware:
    files:
      - generated.hex: {file_type: user}
targets:
  sim_toy:
    default_tool: icarus
    filesets: [rtl, tb, firmware]
    toplevel: toy_tb
""",
        encoding="utf-8",
    )
    project = root / ".booley_project"
    (project / "hooks").mkdir(parents=True)
    (project / "hooks" / "post-setup.sh").write_text(
        "#!/bin/sh\nprintf 'firmware\\n' > generated.hex\n", encoding="utf-8"
    )
    (project / "booley.toml").write_text(
        "[flows.sim]\ndefault_target = 'sim_toy'\n", encoding="utf-8"
    )
    (project / "tickets" / "board" / "queue").mkdir(parents=True)
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project/booley.toml", ".booley_project/hooks")
    _git(root, "commit", "-m", "demo")
    criteria = {"mandatory": {"sim_pass": ["tb/toy_tb.sv @ sim_toy @ smoke @ pass -> pass"]}}
    tio = TicketIO(project / "tickets", project_root=root)
    draft = tio.create_ticket_file(
        "demo",
        TicketFileSpec(
            summary="Demo",
            ticket_type="verification",
            branch="main",
            scope=["rtl/toy.sv"],
            criteria=criteria,
            body="## Description\n\nVerify the demo.\n",
        ),
    )
    assert draft is not None
    assert tio.enqueue_ticket("demo") is True
    workspace = _remove_ticket_worktree(root)
    assert not workspace.exists()
    ticket = project / "tickets" / "board" / "queue" / "demo.md"
    ticket_before = ticket.read_bytes()
    resolved_roots: list[Path] = []

    def resolve_checkout(selected_root: Path) -> Path:
        resolved_roots.append(selected_root)
        return project

    monkeypatch.setattr(
        readiness_module,
        "resolve_checkout_project_dir",
        resolve_checkout,
        raising=False,
    )

    result = check_ticket_ready(root, "demo")

    assert result.errors == ()
    assert resolved_roots == [root.resolve()]
    assert ticket.read_bytes() == ticket_before
    assert not (project / "tickets" / "board" / "active" / "demo.md").exists()
    assert (root / "generated.hex").is_file()
    assert "generated.hex" in fusesoc_registry.target_referenced_files(root, "sim_toy")
    assert _git(root, "status", "--porcelain") == ""


def test_checkout_status_failure_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "demo"
    root.mkdir()

    def failed_status(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["git", "status"],
            returncode=128,
            stdout="",
            stderr="fatal: not a repository",
        )

    monkeypatch.setattr(readiness_module.subprocess, "run", failed_status)

    with pytest.raises(RuntimeError, match="not a repository"):
        readiness_module._checkout_statuses(root)


def test_readiness_without_worktree_checks_current_generation_ref(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    project = root / ".booley_project"
    (project / "tickets/board/drafts").mkdir(parents=True)
    (project / ".gitignore").write_text("/worktrees/\n/.runtime/\n", encoding="utf-8")
    (project / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "initial")
    tio = TicketIO(project / "tickets", project_root=root)
    ticket = tio.create_ticket_file(
        "changed-controls",
        TicketFileSpec(
            summary="Changed controls",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("changed-controls") is True
    workspace = project / "worktrees/changed-controls"
    (workspace / ".booley_project/booley.toml").write_text(
        "[flows]\nchanged = true\n", encoding="utf-8"
    )
    _git(workspace, "add", "-f", ".booley_project/booley.toml")
    _git(workspace, "commit", "-m", "change protected input")
    _remove_ticket_worktree(root)

    result = check_ticket_ready(root, "changed-controls")

    assert result.ready is False
    assert any("protected path" in error for error in result.errors)


def test_worktree_discovery_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "demo"
    root.mkdir()

    def failed_worktree(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["git", "worktree", "list"],
            returncode=128,
            stdout="",
            stderr="fatal: worktree metadata is unreadable",
        )

    monkeypatch.setattr(readiness_module.subprocess, "run", failed_worktree)

    with pytest.raises(
        readiness_module.ReadinessInspectionError,
        match="worktree metadata is unreadable",
    ):
        readiness_module._worktree_for_ref(root, "refs/heads/main")


def test_executable_readiness_uses_authoritative_basis_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "demo"
    (root / ".git").mkdir(parents=True)
    tickets = root / ".booley_project/tickets"

    def reject_missing_receipt(*_args: object, **_kwargs: object) -> None:
        from booley.ticket_board.acceptance_basis import AcceptanceBasisError

        raise AcceptanceBasisError("Acceptance Basis receipt mismatch")

    monkeypatch.setattr(TicketIO, "load_basis", reject_missing_receipt)
    errors = readiness_module._validate_checkout_basis(
        root,
        tickets,
        "demo",
        {"acceptance_basis": {"schema": 1}},
    )

    assert errors == ["Acceptance Basis receipt mismatch"]
