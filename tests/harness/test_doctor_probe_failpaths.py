"""FAIL and WARN branches of Doctor probes, driven through a whole Doctor run.

Each test answers the commands Doctor runs through the shared fake in
``tests/harness/test_doctor.py`` and lets every Booley step between the run and
that fake execute.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import ClassVar

import pytest

import booley
from booley.audit import host_environment
from booley.audit.diagnostic_results import Severity
from booley.harness import doctor
from booley.runtime import agent_config as runtime_agent_config
from booley.runtime.version_attribution import VersionAttribution, VersionOrigin
from tests.diagnostic_helpers import _Rec, _record_environment
from tests.harness.doctor_host_support import serve_healthy_doctor_host
from tests.harness.test_doctor import _doctor_run, _messages, _write_project
from tests.subprocess_budget import run_bounded_child


def _git(root: Path, *args: str) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    run_bounded_child(["git", *identity, *args], cwd=root, check=True)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[list[str]]]:
    """A committed Doctor Project on a healthy host; returns its root and Doctor's commands.

    The Project sits below the working directory, so Doctor resolves Git's relative paths.
    """
    root = tmp_path / "project"
    root.mkdir()
    _write_project(root)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "project")
    return root, serve_healthy_doctor_host(monkeypatch, root)


@pytest.mark.parametrize(
    ("cli", "row"),
    [
        (
            "codex",
            (
                Severity.WARN,
                "worker backend (Codex): Codex CLI not found on PATH (npm i -g @openai/codex)",
            ),
        ),
        (
            "claude",
            (
                Severity.NOTE,
                "worker backend configured locally: Claude SDK; provider authorization was not "
                "exercised and the CLI was not started by plain Doctor "
                "(run `booley doctor --deep` for a live check)",
            ),
        ),
    ],
    ids=["warning", "healthy"],
)
def test_backend_health_row(project, cli, row):
    root, _calls = project
    config = root / ".booley" / "booley.toml"
    # Bytes keep LF on Windows, where Doctor would report a CRLF checkout.
    config.write_bytes(
        config.read_bytes().replace(b"[agent]", f'[agent]\ndefault = "{cli}"'.encode())
    )

    result = _doctor_run(root)

    assert [
        (f.severity, f.message) for f in result.findings if f.message.startswith("worker backend")
    ] == [row]


def test_backend_config_failure_is_a_warning_not_a_crash(project, monkeypatch):
    root, _calls = project

    def unreadable(_root):
        raise ValueError("models.toml is garbage")

    # Project load validates [agent] first, so no Project file makes this later load fail.
    monkeypatch.setattr(runtime_agent_config, "load_backend_config", unreadable)

    warnings = _messages(_doctor_run(root), Severity.WARN, check_id="agent.backend-health")

    assert warnings == ["worker backend health check failed: models.toml is garbage"]


@pytest.mark.parametrize("dirty", [True, False], ids=["merge-and-dirty", "clean"])
def test_git_state_reports_an_unfinished_merge_and_a_dirty_tree(project, dirty):
    root, _calls = project
    if dirty:
        (root / ".git" / "MERGE_HEAD").write_bytes(b"0" * 40 + b"\n")
        (root / "rtl" / "dut.sv").write_bytes(b"module dut; wire w; endmodule\n")
        (root / "untracked.txt").write_bytes(b"new\n")

    result = _doctor_run(root)

    git_rows = [
        (finding.severity, finding.message)
        for finding in result.findings
        if "git operation" in finding.message
        or "merge/rebase" in finding.message
        or "git working tree" in finding.message
    ]
    assert git_rows == (
        [
            (Severity.NOTE, "git working tree has 2 modified file(s)"),
            (Severity.FAIL, "git operation in progress: merge"),
        ]
        if dirty
        else [(Severity.PASS, "no git merge/rebase/cherry-pick in progress")]
    )


def test_sim_dry_run_pins_the_first_test_of_the_selected_cores_section(project):
    """#1595: a qualified Target reads its own core's tests.toml section, not a namesake's."""
    root, calls = project
    (root / "other.core").write_bytes(
        b"CAPI=2:\nname: ::other:0\nfilesets:\n  rtl:\n    files: [rtl/dut.sv]\n"
        b"    file_type: systemVerilogSource\ntargets:\n  sim_fast:\n    flow: sim\n"
        b"    flow_options: {tool: verilator}\n    filesets: [rtl]\n    toplevel: dut\n"
    )
    (root / ".booley" / "tests.toml").write_bytes(
        b'["::other:0#sim_fast"]\ntests = ["other_first"]\n'
        b'["::unit:0#sim_fast"]\ntests = ["unit_first"]\n'
    )

    _doctor_run(root)

    [dry_run] = [argv for argv in calls if "booley.flows.sim" in argv and "--dry-run" in argv]
    assert dry_run[dry_run.index("--target") + 1] == "unit#sim_fast"
    assert dry_run[dry_run.index("--test") + 1] == "unit_first"


@pytest.mark.parametrize(
    ("source_checkout", "fix"),
    [
        (False, "fix .booley/mcp_tools and the [flows]/[specialists] tables in booley.toml"),
        (True, "run Doctor from the Project's workspace, not a Booley source checkout"),
    ],
    ids=["retired-config", "source-checkout"],
)
def test_custom_endpoint_validation_failure_fails_with_its_fix(tmp_path, source_checkout, fix):
    (tmp_path / ".booley").mkdir()
    if source_checkout:
        (tmp_path / "pyproject.toml").write_bytes(b"[tool.booley]\nsource_checkout = true\n")
    else:
        (tmp_path / ".booley" / "booley.toml").write_bytes(
            b"[mcp_tools.custom_check]\nenabled = true\n"
        )
    rec = _Rec()

    # Private: a whole Doctor run rejects this config at project load and refuses a
    # Source Checkout before any check, so only the check itself reaches these branches.
    doctor._check_custom_endpoints(tmp_path, rec.p, rec.w, rec.f)

    assert rec.kinds() == {"fail"}
    assert rec.fails()[0].startswith("custom endpoint validation failed: ")
    assert rec.fix_hints == [fix]


class _FakeDist:
    metadata: ClassVar[dict[str, str]] = {"Version": "0.0.9"}


@pytest.mark.parametrize(
    ("installed", "dist_name", "expected"),
    [
        ({"booley"}, "booley", ("fail", "booley-rtl` is not installed at all", "0.0.9")),
        ({"booley", "booley-rtl"}, "booley-rtl", ("fail", "both `booley`", "shadow")),
        (set(), "booley-rtl", ("pass",)),
    ],
    ids=["legacy-only", "both-shadowing", "current-only"],
)
def test_legacy_distribution_audit(monkeypatch, installed, dist_name, expected):
    def distribution(name):
        if name in installed:
            return _FakeDist()
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    monkeypatch.setattr(booley, "__dist_name__", dist_name)
    monkeypatch.setattr(
        booley,
        "version_attribution",
        VersionAttribution(
            version="1.0.0", origin=VersionOrigin.DISTRIBUTION, distribution_name=dist_name
        ),
    )
    rec = _Rec()
    _record_environment(host_environment.audit_legacy_distribution(), passed=rec.p, failed=rec.f)
    [(kind, message)] = rec.events
    assert kind == expected[0]
    assert all(text in message for text in expected[1:])
