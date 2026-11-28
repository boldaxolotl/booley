"""A healthy host for a whole Doctor run on a Project that has no Git repository.

Git is answered by ``GitCommandFake`` rules; every command no rule matches is answered here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from booley import __version__
from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.core.resources import package_data_dir
from booley.harness import doctor, host_diagnostics
from booley.harness import image_lifecycle as harness_lifecycle
from booley.runtime import devcontainer as dc
from booley.runtime import image_lifecycle as lifecycle
from booley.runtime import incontainer_setup, runtime_context, session_issuance
from booley.runtime.booley_dir import reset_cache
from booley.runtime.runtime_context import RuntimeLocation
from tests.diagnostic_helpers import _issued_runtime_state
from tests.docker_cli_guard import (
    route_bounded_captures_through_subprocess_run,
    route_docker_helper_through_subprocess_run,
)
from tests.docker_host_support import fake_docker_host
from tests.git_command_fake import GitCommandFake
from tests.sandbox_venue_fake import place

_MCP_TOOLS = ["synth", "bwave", "coverage_analyst", "lint", "mutation", "reviewer", "sim"]


def t3_pin() -> str:
    """Return the T3 Code version the packaged agent CLI manifest pins."""
    manifest = package_data_dir() / "docker" / "agent-clis-package.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["t3code"]["version"]


# T3 Code's preview browser version record, then the Sandbox Image browser's.
BROWSER_RECORDS = [
    "cat",
    "/opt/t3code/preview-browser-version",
    "/opt/chrome-headless-shell/VERSION",
]


def patch_doctor_environment(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    booley_dir: Path,
    *,
    mcp_tools: list[str] | None = None,
    mcp_payload: dict | None = None,
    runtime_booley_version: str = __version__,
) -> list[list[str]]:
    """Answer a whole Doctor run's host and Sandbox as healthy; return every command it ran."""
    reset_cache()
    route_bounded_captures_through_subprocess_run(monkeypatch)
    # Modules that import this helper lack test_doctor's autouse routing; without
    # it Doctor's container checks start real `docker run`s that can each wait 30 s.
    route_docker_helper_through_subprocess_run(monkeypatch)
    fake_docker_host(monkeypatch)
    # Never probe this machine's own T3 Code port.
    monkeypatch.setattr(incontainer_setup, "t3_state", lambda: "off")
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    # Doctor tests exercise the host-side orchestration path deterministically,
    # whatever machine the suite itself runs on.
    _patch_host_environment(monkeypatch, root)
    monkeypatch.setattr(doctor.idk, "image_exists", lambda *_a, **_kw: True)
    monkeypatch.setattr(doctor.idk, "image_id", lambda image: image)
    # A whole run cannot supply a host Sandbox Image graph; the provenance tests override the CHECK.
    answer_image_check(monkeypatch, lifecycle.Status.CURRENT)
    # The rebuild of the spec `booley init` would write has its own tests; here the
    # seeded fixture spec stands in for it.
    monkeypatch.setattr(
        doctor,
        # Private: the real call builds an image spec through `init_cmd`; a full run cannot supply a host image.
        "_expected_sandbox_spec",
        lambda project_root: (
            lambda _image: json.loads(
                dc.devcontainer_path(project_root).read_text(encoding="utf-8")
            )
        ),
    )

    # Orchestration tests substitute the public issuance interface. Runtime
    # owner tests separately exercise real authority and live-state policy.
    issuance, _spec, _labels, _state = _issued_runtime_state(root)
    issuance = replace(issuance, image=dc.SANDBOX_IMAGE)
    monkeypatch.setattr(session_issuance, "validate", lambda *_args: issuance)
    monkeypatch.setattr(session_issuance, "requested_license", lambda _root: None)
    monkeypatch.setattr(
        doctor.session_runtime, "up_without_recovery", lambda _root: "booley-session-test"
    )
    monkeypatch.setattr(
        doctor.idk,
        "compare_issued_build",
        lambda *_args, **_kwargs: doctor.image_identity.Comparison(
            doctor.image_identity.Status.MATCH
        ),
    )
    payload = mcp_payload or {
        "tools": mcp_tools or _MCP_TOOLS,
        "errors": [],
        "logs_dir": "/work/.booley/job_logs/session",
        "logs_dir_ok": True,
    }
    # The stdlib ``subprocess.run`` also carries the MCP server inspection owner's probe
    # (``booley.mcp.server_inspection``).
    fake = GitCommandFake.install(monkeypatch)
    fake.others = lambda cmd, **_kw: _other_command(cmd, payload, runtime_booley_version)
    _answer_git(fake, root)
    return fake.commands


def _answer_git(fake: GitCommandFake, root: Path) -> None:
    """Answer Git as a repository at *root* that tracks nothing; rules match in order."""
    answer = partial(fake.answer, returncode=0, stderr=b"")
    top, git_dir = f"{root}\n".encode(), os.fsencode(root / ".git")
    answer("-C", "--show-toplevel", stdout=top)
    answer("-C", "config", "core.autocrlf", stdout=b"false\n")
    answer("-C", "ls-files", "--eol")
    answer("-C", "status")
    # Healthy default: the ADR 0028 worktree prune guard is set.
    answer("config", "gc.worktreePruneExpire", stdout=b"never\n")
    answer("config", "worktree.useRelativePaths", stdout=b"true\n")
    answer("config", "extensions.relativeWorktrees", returncode=1)
    answer("-C", stdout=b"ok\n")  # every other `git -C` command, as the default answers
    answer("rev-parse", "--is-inside-work-tree", stdout=b"true\n")
    answer("rev-parse", "--show-toplevel", stdout=top)
    answer("rev-parse", "--git-dir", stdout=git_dir)
    answer("rev-parse", "--git-common-dir", stdout=git_dir)
    answer("status", "--porcelain")
    answer("--version", stdout=b"git version 2.53.0\n")
    answer("ls-files")
    answer("check-ignore", returncode=1)


def _other_command(cmd, payload: dict, runtime_booley_version: str):  # noqa: PLR0911 - command table
    """Answer what no Git rule did: Docker, the Sandbox's probes, or "ok"."""
    if cmd[:3] == [sys.executable, "-c", "import booley.runtime"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if cmd[1] == "ps":
        # Live Sandbox inventory must see an empty container list.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if cmd[1:3] == ["run", "--rm"] and cmd[-2:] == ["id", "-u"]:
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{os.getuid()}\n", stderr="")
    if cmd[1:3] == ["run", "--rm"] and cmd[-2:] == ["git", "--version"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="git version 2.53.0\n", stderr="")
    if cmd[-2:] == ["t3", "--version"]:
        # The Sandbox Image the fake stands for is built from the packaged manifest.
        return subprocess.CompletedProcess(cmd, 0, stdout=f"t3 v{t3_pin()}\n", stderr="")
    if cmd[-3:] == BROWSER_RECORDS:
        return subprocess.CompletedProcess(cmd, 0, stdout="1.2.3.4\n1.2.3.4\n", stderr="")
    if cmd[1:3] == ["image", "ls"]:
        # No issued Sandbox Image keepers; the strict listing rejects stray output.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if cmd[1:3] == ["image", "inspect"] and "{{json .Config.Env}}" in cmd:
        # Healthy default: the sandbox image bakes the ADR 0028 runtime marker.
        return subprocess.CompletedProcess(cmd, 0, stdout='["BOOLEY_CONTAINER=1"]\n', stderr="")
    if cmd[1:3] == ["image", "inspect"] and "--format" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout='"2099-01-01T00:00:00Z"\n', stderr="")
    if "import booley; print(booley.__version__)" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{runtime_booley_version}\n", stderr="")
    if cmd[-3:] == ["python", "-m", "booley.mcp.probe"]:
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
    if "CORE_RESOLVE_JSON" in " ".join(str(part) for part in cmd):
        # The deep .core-resolvability snippet (Sandbox command): report the
        # selected Targets as resolvable via the marker line it scrapes.
        selected = json.loads(cmd[-1])
        verdicts = json.dumps([{"selector": item["selector"], "ok": True} for item in selected])
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"[[CORE_RESOLVE_JSON]]{verdicts}\n", stderr=""
        )
    return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")


def answer_image_check(
    monkeypatch: pytest.MonkeyPatch,
    status: lifecycle.Status | None = None,
    *,
    raises: Exception | None = None,
    diagnostics: tuple[str, ...] = (),
    selected_id: str = dc.SANDBOX_IMAGE,
) -> None:
    """Stand in for the lifecycle CHECK `booley init --check-only` runs."""

    def reconcile(_scope, _intent):
        if raises:
            raise raises
        found = tuple(lifecycle.Diagnostic("stale", m) for m in diagnostics)
        return lifecycle.LifecycleResult(dc.SANDBOX_IMAGE, selected_id, status, diagnostics=found)

    monkeypatch.setattr(harness_lifecycle, "reconcile_planned", reconcile)


def _patch_host_environment(monkeypatch, root: Path) -> None:
    place(monkeypatch, RuntimeLocation.HOST)
    # The suite itself may run under an agent CLI (CLAUDECODE, CODEX_HOME, ...);
    # a plain host shell is the baseline these fixtures model.
    monkeypatch.setattr(runtime_context, "agent_session_app", lambda: None)
    # The fixture Project pins `auth = "subscription"`, which needs a credential to pass.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fixture")
    monkeypatch.setattr(Path, "home", lambda: _write_skills_home(root))
    runtime = "doc" + "ker"
    monkeypatch.setattr(doctor.shutil, "which", lambda name: runtime if name == runtime else None)
    report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "Host Bootstrap docker: ok"),))
    monkeypatch.setattr(
        host_diagnostics,
        "inspect_host",
        lambda _root: host_diagnostics.HostDiagnosticResult(report, "docker"),
    )


def _write_skills_home(root: Path) -> Path:
    home = root / "home"
    (home / ("." + "ag" + "ents") / "skills" / "one").mkdir(parents=True, exist_ok=True)
    return home
