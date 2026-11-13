"""Composition keeps report policy and effect ordering at Doctor's public entry."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.audit import host_environment
from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.audit.doctor_waivers import load_doctor_waivers
from booley.config.host_config import SandboxHostPolicy
from booley.config.project_config import normalize_tests_toml
from booley.harness import doctor, doctor_deep, host_diagnostics
from booley.harness.setup import readiness
from booley.runtime import doctor_stamp, inspection, session_admission, vaporview
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError
from tests.conftest import symlink_or_skip
from tests.diagnostic_helpers import _Rec
from tests.docker_cli_guard import route_docker_helper_through_subprocess_run
from tests.harness.test_doctor import (
    _disable_line_ending_stealth,
    _patch_environment,
    _write_project,
)
from tests.subprocess_budget import run_bounded_child


@pytest.fixture(autouse=True)
def _docker_helper_uses_subprocess_run(monkeypatch):
    """This module fakes the Docker CLI through ``subprocess.run``."""
    route_docker_helper_through_subprocess_run(monkeypatch)


# The exact host-wide Sandbox inventory listing owned by session_admission.
_INVENTORY_ARGV = [
    "docker",
    "ps",
    "--filter",
    "label=booley.role=interactive",
    "--format",
    "{{.ID}}\t{{.Names}}",
]


def _canonical_host_installation():
    return SimpleNamespace(
        version="0.2.15",
        revision="",
        payload_fingerprint="b" * 64,
        executable="/usr/local/bin/booley",
        interpreter="/usr/local/bin/python3",
        distribution_root="/opt/booley",
    )


def _use_real_host_checks(monkeypatch, root, booley_dir, bootstrap_result):
    """Run Doctor with the real host checks over a stubbed Bootstrap result."""
    inspect_host = host_diagnostics.inspect_host
    calls = _patch_environment(monkeypatch, root, booley_dir)
    monkeypatch.setattr(host_diagnostics, "inspect_host", inspect_host)
    monkeypatch.setattr(host_diagnostics, "load_host_installation", _canonical_host_installation)
    monkeypatch.setattr(
        host_diagnostics,
        "current_host_installation",
        lambda _source: _canonical_host_installation(),
    )
    monkeypatch.setattr(
        host_diagnostics.bootstrap, "reconcile_bootstrap", lambda _intent: bootstrap_result
    )
    monkeypatch.setattr(
        host_environment,
        "probe_host_clock",
        lambda: host_environment.EnvironmentFinding(
            host_environment.EnvironmentSeverity.SKIP, "offline"
        ),
    )
    return calls


def _sandbox_document(name: str, project_root: Path, *, editor: bool, egress: str | None) -> dict:
    """A running Booley Sandbox as `docker container inspect` reports it."""
    labels = {"booley.role": "interactive", "booley.project-id": f"id-{name}"}
    if editor:
        labels["devcontainer.local_folder"] = str(project_root)
    env = ["PATH=/usr/bin", "BOOLEY_CONTAINER=1"]
    if egress is not None:
        env.append(f"BOOLEY_EGRESS={egress}")
    return {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "Created": "2026-10-01T10:00:00Z",
        "State": {"Running": True, "StartedAt": "2026-10-01T10:00:01Z"},
        "Config": {"Labels": labels, "Env": env},
        "Mounts": [{"Type": "bind", "Source": str(project_root), "Destination": "/work"}],
    }


def _run_host_doctor(
    monkeypatch, root, *, egress: str, sandboxes: dict[str, dict], listing_fails: bool = False
):
    """Run the real Doctor reporter on the host with a fixture Sandbox inventory."""
    booley_dir = _write_project(root)
    bootstrap = host_diagnostics.bootstrap
    result = bootstrap.BootstrapResult(
        host_diagnostics.Intent.CHECK,
        (bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState.CURRENT, "ok"),),
        policy=SandboxHostPolicy(egress=egress),
    )
    _use_real_host_checks(monkeypatch, root, booley_dir, result)
    fixture_run = subprocess.run
    inventory_calls = []

    def run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if argv == _INVENTORY_ARGV:
            inventory_calls.append(argv)
            if listing_fails:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="daemon unavailable")
            listing = "".join(f"id-{name}\t{name}\n" for name in sandboxes)
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        if argv[:3] == ["docker", "container", "inspect"] and argv[3].startswith("id-"):
            document = sandboxes[argv[3].removeprefix("id-")]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(document), stderr="")
        return fixture_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return doctor.run_doctor_result(argparse.Namespace(deep=False), root), inventory_calls


@pytest.mark.parametrize("egress", ["proxy", "unproxied"])
def test_host_doctor_reports_host_egress_mode(egress, tmp_path, monkeypatch):
    result, inventory_calls = _run_host_doctor(monkeypatch, tmp_path, egress=egress, sandboxes={})
    passed = [f.message for f in result.findings if f.severity == "pass"]
    assert f"host Sandbox egress mode: {egress}" in passed
    assert not [f for f in result.findings if f.check_id == "host.sandbox-egress-mismatch"]
    assert f"0 running Booley Sandbox(es) match host egress mode {egress}" in passed
    assert inventory_calls == [_INVENTORY_ARGV]


@pytest.mark.parametrize(
    ("host", "editor", "stale", "fix_commands"),
    [
        pytest.param(
            "unproxied",
            False,
            None,
            (
                ["booley", "session", "down", "--project"],
                ["booley", "init", "--seed", "--project"],
            ),
            id="host-unproxied-headless-proxy",
        ),
        pytest.param(
            "proxy",
            True,
            "unproxied",
            (["booley", "init", "--seed", "--project"],),
            id="host-proxy-editor-unproxied",
        ),
    ],
)
def test_host_doctor_warns_for_each_mismatched_running_sandbox(
    host, editor, stale, fix_commands, tmp_path, monkeypatch
):
    stale_root = tmp_path / "stale-project"
    current_root = tmp_path / "current-project"
    current_egress = "unproxied" if host == "unproxied" else None
    sandboxes = {
        "stale-sandbox": _sandbox_document(
            "stale-sandbox", stale_root, editor=editor, egress=stale
        ),
        "current-sandbox": _sandbox_document(
            "current-sandbox", current_root, editor=not editor, egress=current_egress
        ),
    }
    result, inventory_calls = _run_host_doctor(
        monkeypatch, tmp_path, egress=host, sandboxes=sandboxes
    )
    mismatches = [f for f in result.findings if f.check_id == "host.sandbox-egress-mismatch"]
    assert len(mismatches) == 1, result.findings
    finding = mismatches[0]
    sandbox_mode = "proxy" if stale is None else stale
    assert (finding.severity, finding.subject) == ("warn", str(stale_root.resolve()))
    assert finding.message == (
        f"Sandbox stale-sandbox (Project {stale_root.resolve()}) runs with egress "
        f"'{sandbox_mode}' but host [sandbox] egress = '{host}'"
    )
    # Each recreate command names the stale Project, quoted for the host shell
    # (POSIX shells or PowerShell on Windows).
    for command in fix_commands:
        assert session_admission._command([*command, str(stale_root.resolve())]) in finding.fix
    assert ("Rebuild Container" in finding.fix) is editor
    assert result.exit_code == 0
    assert inventory_calls == [_INVENTORY_ARGV]


def test_host_doctor_fails_when_sandbox_inventory_is_unreadable(tmp_path, monkeypatch):
    result, inventory_calls = _run_host_doctor(
        monkeypatch, tmp_path, egress="unproxied", sandboxes={}, listing_fails=True
    )
    failures = [
        f
        for f in result.findings
        if f.severity == "fail"
        and f.message.startswith("cannot check running Sandboxes against host egress mode")
    ]
    assert len(failures) == 1, result.findings
    assert "daemon unavailable" in failures[0].message
    assert result.exit_code != 0
    assert inventory_calls == [_INVENTORY_ARGV]


@pytest.mark.parametrize("waived", [False, True])
def test_pending_bootstrap_completes_with_waivable_warning_and_correct_health(
    waived, tmp_path, monkeypatch
):
    booley_dir = _write_project(tmp_path)
    _disable_line_ending_stealth(tmp_path)
    bootstrap = host_diagnostics.bootstrap
    _use_real_host_checks(
        monkeypatch,
        tmp_path,
        booley_dir,
        bootstrap.BootstrapResult(
            host_diagnostics.Intent.CHECK,
            (
                bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState.CURRENT, "ok"),
                bootstrap.BootstrapFinding(
                    "skills", bootstrap.BootstrapState.PENDING, "stale links"
                ),
            ),
        ),
    )
    waivers = "version = 1\n"
    for check in ("fusesoc.worktree-core-shadow", "project.core-file-untracked"):
        waivers += (
            f'[[waiver]]\ncheck = "{check}"\n'
            'reason = "Synthetic project fixture"\npermanent = true\n'
        )
    if waived:
        waivers += (
            '[[waiver]]\ncheck = "host.bootstrap-pending"\n'
            'subject = "skills"\nreason = "Reviewed temporary gap"\npermanent = true\n'
        )
    (booley_dir / "doctor-waivers.toml").write_text(waivers)
    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)
    finding = next(f for f in result.findings if f.check_id == "host.bootstrap-pending")
    assert (finding.severity, finding.subject) == ("waived" if waived else "warn", "skills")
    assert result.exit_code == 0
    assert result.clean is waived
    assert doctor_stamp.stamp_path(booley_dir).exists() is waived


def test_typed_report_preserves_warning_deduplication_and_machine_fields():
    reporter = doctor._Reporter.create()
    first = DiagnosticFinding(
        Severity.WARN, "first wording", "repair", "project.example", "target", "same"
    )
    second = DiagnosticFinding(
        Severity.WARN, "new wording", "repair", "project.example", "target", "same"
    )
    reporter.diagnostics(DiagnosticReport((first, second)))
    result = reporter.result(0)
    assert result.counts["warn"] == 1
    assert result.findings == (
        DiagnosticFinding(Severity.WARN, "first wording", "repair", "project.example", "target"),
    )


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_project_observations_and_repairs_keep_the_existing_order(
    tmp_path, monkeypatch, read_only, valid
):
    booley_dir = _write_project(tmp_path)
    (booley_dir / "AGENTS.md").write_text("# Project guidance\n")
    if not valid:
        (booley_dir / "booley.toml").unlink()
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    events = []

    def trace(module, name, label):
        original = getattr(module, name)

        def run(*args, **kwargs):
            events.append(label)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, run)

    trace(readiness, "load_project", "config")
    trace(doctor, "_check_upgrade_review", "upgrade")
    trace(readiness, "check_guidance", "guidance")
    trace(doctor, "_check_project_data_destination_branch", "project-data-branch")
    trace(doctor, "_check_worktree_prune_guard", "prune")
    trace(readiness, "inspect_line_endings", "line-endings")
    trace(doctor, "_check_worktree_core_shadow_guard", "shadow")
    trace(readiness, "check_stealth_cores", "projections")
    doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path, read_only=read_only)
    expected = [
        "config",
        "upgrade",
        "guidance",
        "project-data-branch",
        "prune",
        "line-endings",
        "shadow",
        "projections",
    ]
    if not valid:
        expected = ["config", "upgrade", "prune", "line-endings"]
    assert events == expected
    assert (tmp_path / "AGENTS.md").exists() is (valid and not read_only)


def _owner_frame(messages: list[str], owner: str) -> tuple[int, int]:
    """Indices of the Doctor rows that bracket an owner's rows in the rendered report."""
    if owner == "cocotb":
        # Real Doctor rows frame the owner: Targets are enumerated first and the
        # Target source checks follow, with the owner's rows contiguous between.
        before = next(
            i for i, m in enumerate(messages) if m.startswith(".core Targets enumerated")
        )
        after = next(
            i for i, m in enumerate(messages) if m == "sim Target 'sim_fast' TB fileset tagged"
        )
        return before, after
    return messages.index("before-marker"), messages.index("after-marker")


@pytest.mark.parametrize("owner", ["line-endings", "interactive-git", "cocotb", "viewer-bridge"])
def test_doctor_renders_owner_report_in_place(tmp_path, monkeypatch, owner):
    """An owner's report is rendered where the old inline check ran, waivers applied."""
    booley_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)

    def marker(label):
        return DiagnosticReport((DiagnosticFinding(Severity.PASS, label),))

    if owner == "line-endings":
        check_id, subject = "git.line-endings-unreadable", "project-checkout"
        stub = lambda *_a, **_k: _owner_report(check_id, subject)  # noqa: E731
        monkeypatch.setattr(readiness, "inspect_line_endings", stub)
        monkeypatch.setattr(
            doctor, "_check_worktree_prune_guard", lambda _root, ok, *_a: ok("before-marker")
        )
        monkeypatch.setattr(
            doctor,
            "_check_worktree_core_shadow_guard",
            lambda _dir, ok, *_a, **_k: ok("after-marker"),
        )
    elif owner == "cocotb":
        check_id, subject = "cocotb.tb-tag-missing", "sim_fast"
        monkeypatch.setattr(
            _cocotb_owner(),
            "inspect_cocotb_targets",
            lambda _request: _owner_report(check_id, subject),
        )
    elif owner == "viewer-bridge":
        check_id, subject = "project.git-excludes-missing", ".booley"
        requests: list[vaporview.ViewerBridgeRequest] = []

        def inspect_bridge(request):
            requests.append(request)
            return _owner_report(check_id, subject)

        monkeypatch.setattr(vaporview, "inspect_viewer_bridge", inspect_bridge)
        # Public owner calls bracket the Viewer Bridge (credential checks sit between).
        monkeypatch.setattr(
            inspection, "inspect_interactive_git", lambda *_a, **_k: marker("before-marker")
        )
        monkeypatch.setattr(
            inspection, "inspect_retained_resources", lambda *_a, **_k: marker("after-marker")
        )
    else:
        check_id, subject = "project.git-excludes-missing", ".booley"
        stub = lambda *_a, **_k: _owner_report(check_id, subject)  # noqa: E731
        monkeypatch.setattr(inspection, "inspect_interactive_git", stub)
        monkeypatch.setattr(
            inspection,
            "inspect_runtime",
            lambda _request: SimpleNamespace(report=marker("before-marker")),
        )
        monkeypatch.setattr(
            doctor, "_run_agent_credential_checks", lambda _p, r: r.pass_("after-marker")
        )
    (booley_dir / "doctor-waivers.toml").write_text(
        f'version = 1\n[[waiver]]\ncheck = "{check_id}"\nsubject = "{subject}"\n'
        'reason = "Accepted for this fixture"\npermanent = true\n'
    )

    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)

    messages = [f.message for f in result.findings]
    owned_pass, owned_warn = messages.index("owner pass"), messages.index("owner warn")
    before, after = _owner_frame(messages, owner)
    if owner == "cocotb":
        assert owned_warn == owned_pass + 1
    assert before < owned_pass < owned_warn < after
    waived = result.findings[owned_warn]
    assert (waived.severity, waived.check_id, waived.subject) == (
        Severity.WAIVED,
        check_id,
        subject,
    )
    assert result.exit_code == 0
    if owner == "viewer-bridge":
        assert requests == [vaporview.ViewerBridgeRequest(tmp_path, "docker")]


def test_doctor_renders_tests_toml_report_after_core_security(tmp_path, monkeypatch):
    """The tests.toml owner's report follows the core security finding, waived through Doctor."""
    # Imported here so an absent owner fails this test body, not the module's collection.
    from booley.flows import target_test_plan_inspection

    booley_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    check_id, subject = "sim.tests-toml-fixture", "tests.toml"
    requests = []

    def stub(request):
        requests.append(request)
        return _owner_report(check_id, subject)

    monkeypatch.setattr(target_test_plan_inspection, "inspect_target_test_plans", stub)
    (booley_dir / "doctor-waivers.toml").write_text(
        f'version = 1\n[[waiver]]\ncheck = "{check_id}"\nsubject = "{subject}"\n'
        'reason = "Accepted for this fixture"\npermanent = true\n'
    )

    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)

    # The request carries the checkout's Project Root and Booley Directory only.
    assert requests == [
        target_test_plan_inspection.TargetTestPlanInspectionRequest(tmp_path, booley_dir)
    ]
    messages = [f.message for f in result.findings]
    owned_pass, owned_warn = messages.index("owner pass"), messages.index("owner warn")
    assert owned_warn == owned_pass + 1
    # The finding right before the owner's is the real core-audit security finding.
    assert messages[owned_pass - 1].startswith(".core security validation passed (")
    waived = result.findings[owned_warn]
    assert (waived.severity, waived.check_id, waived.subject) == (
        Severity.WAIVED,
        check_id,
        subject,
    )
    assert result.exit_code == 0


def _owner_report(check_id: str, subject: str) -> DiagnosticReport:
    """A PASS followed by a subject-scoped WARN, as an owner would return them."""
    return DiagnosticReport(
        (
            DiagnosticFinding(Severity.PASS, "owner pass"),
            DiagnosticFinding(
                Severity.WARN, "owner warn", "owner fix", check_id=check_id, subject=subject
            ),
        )
    )


_COCOTB_AUDIT_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb: {files: [tb/test_a.py], tags: [tb]}
  tb_untagged: {files: [tb/test_b.py]}
targets:
  sim_a:
    flow: sim
    flow_options: {tool: verilator, cocotb_module: test_a, booley: {doctor: [sim]}}
    filesets: [rtl, tb]
    toplevel: dut
  sim_b:
    flow: sim
    flow_options: {tool: verilator, cocotb_module: test_b}
    filesets: [rtl, tb_untagged]
    toplevel: dut
"""


def _cocotb_audit_project(root: Path) -> doctor.ProjectAudit:
    """A Project with two cocotb Targets that between them earn every cocotb row kind.

    ``sim_a`` has a tagged module that defines only ``test_reset`` yet a
    qualified tests.toml section names ``test_typo`` too and declares ``select``;
    ``sim_b`` sits in an untagged fileset and its module is not on disk.
    """
    booley_dir = root / ".booley"
    booley_dir.mkdir()
    (root / "design.core").write_text(_COCOTB_AUDIT_CORE, encoding="utf-8", newline="\n")
    (root / "rtl").mkdir()
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (root / "tb").mkdir()
    (root / "tb" / "test_a.py").write_text(
        "import cocotb\n\n@cocotb.test()\nasync def test_reset(dut):\n    pass\n",
        encoding="utf-8",
        newline="\n",
    )
    tests_toml = '["demo#sim_a"]\nselect = "+t={name}"\ntests = ["test_reset", "test_typo"]\n'
    (booley_dir / "tests.toml").write_text(tests_toml, encoding="utf-8", newline="\n")
    (booley_dir / "doctor-waivers.toml").write_text(
        'version = 1\n[[waiver]]\ncheck = "cocotb.tb-tag-missing"\nsubject = "sim_b"\n'
        'reason = "Accepted for this fixture"\npermanent = true\n',
        encoding="utf-8",
        newline="\n",
    )
    return doctor.ProjectAudit(
        project_root=root,
        booley_dir=booley_dir,
        booley_toml={},
        configs_toml={"x": {}},
        first_target="x",
    )


def _cocotb_owner():
    """The cocotb owner module, imported where used."""
    return importlib.import_module("booley.flows.sim.backends.cocotb_target_checks")


def test_cocotb_core_audit_output_is_pinned(tmp_path):
    """The cocotb rows keep their text, severity, IDs, subjects, order, waiver and exit code.

    The literals were captured from Doctor's own cocotb checks before they moved
    to their Flow owner and are unchanged by the move.
    """
    project = _cocotb_audit_project(tmp_path)
    reporter = doctor._Reporter.create(load_doctor_waivers(project.booley_dir))
    doctor._run_core_audit(
        project,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _note=reporter.note_,
        _diagnostics=reporter.diagnostics,
    )
    exit_code = reporter.finish()

    rows = [(f.severity, f.message, f.fix, f.check_id, f.subject) for f in reporter.findings]
    last_before = (Severity.PASS, "2 Verilator sim Target(s) trace-capable (own a --exe main)")
    first_after = (Severity.PASS, "sim Target 'sim_a' TB fileset tagged")
    start = [row[:2] for row in rows].index(last_before)
    assert rows[start + 6][:2] == first_after
    assert rows[start + 1 : start + 6] == [
        (
            Severity.FAIL,
            "cocotb Target 'sim_a' declares a tests.toml `select` plusarg template",
            "remove `select` — cocotb test selection is the COCOTB_TEST_FILTER env var "
            "built from the `tests` list (ADR 0034)",
            None,
            None,
        ),
        (Severity.PASS, "cocotb Target 'sim_a' module file present (test_a.py)", "", None, None),
        (
            Severity.WARN,
            "cocotb Target 'sim_a': tests.toml names not found as functions decorated with "
            "@cocotb.test in test_a.py: test_typo (static, advisory)",
            "check the names against the @cocotb.test functions — a mismatch surfaces at "
            "run time as inconclusive",
            "cocotb.test-name-missing",
            "sim_a",
        ),
        (
            Severity.WAIVED,
            "cocotb Target 'sim_b': the fileset carrying test_b.py is not tagged tags:[tb]",
            "Accepted for this fixture",
            "cocotb.tb-tag-missing",
            "sim_b",
        ),
        (
            Severity.WARN,
            f"cocotb Target 'sim_b': module file {tmp_path / 'tb' / 'test_b.py'} not found on disk",
            "author the Python testbench (or fix the fileset path)",
            "cocotb.module-file-missing",
            "sim_b",
        ),
    ]
    assert reporter.counts["waived"] == 1
    assert exit_code == 1


def test_core_audit_renders_the_owner_report_unchanged(tmp_path, monkeypatch):
    """Doctor builds the owner's request, hands over its report once, and adds no cocotb row."""
    project = _cocotb_audit_project(tmp_path)
    report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "owner-row"),))
    requests = []

    def spy(request):
        requests.append(request)
        return report

    monkeypatch.setattr(_cocotb_owner(), "inspect_cocotb_targets", spy)
    rendered = []
    rec = _Rec()
    doctor._run_core_audit(
        project, rec.p, rec.w, rec.s, rec.f, _note=rec.n, _diagnostics=rendered.append
    )

    (request,) = requests
    assert [target.name for target in request.targets] == ["sim_a", "sim_b"]
    assert request.tests_sections == normalize_tests_toml(
        tomllib.loads((project.booley_dir / "tests.toml").read_text(encoding="utf-8"))
    )
    # The Test Run plan owner also reports through the same path, after the cocotb owner.
    assert [item for item in rendered if item is report] == [report]
    # Its position among the rendered reports: before the Test Run plan report, which
    # is followed only by the native-dependency report.
    assert rendered.index(report) < len(rendered) - 2
    assert not any(message.startswith("cocotb Target") for _level, message in rec.events)


def test_container_tools_ask_the_owner_for_cocotb(tmp_path, monkeypatch):
    """The conditional cocotb container check is gated by the owner's answer."""
    booley_dir = _write_project(tmp_path)
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=booley_dir,
        booley_toml={},
        configs_toml={},
        first_target="sim_fast",
    )
    asked = []

    def owner_says_yes(project_root):
        asked.append(project_root)
        return True

    monkeypatch.setattr(_cocotb_owner(), "project_has_cocotb_target", owner_says_yes)
    checks = []

    def record(description, argv, fix):
        checks.append((description, argv, fix))

    doctor._run_container_tool_checks(project, record)
    assert asked == [project.project_root]
    assert (
        "cocotb in container (a Cocotb Target exists)",
        ["cocotb-config", "--version"],
        "sandbox image predates cocotb support — rebuild the sandbox image",
    ) in checks

    asked.clear()
    checks.clear()
    doctor._run_container_tool_checks(None, record)
    assert asked == []
    assert not any(argv[0] == "cocotb-config" for _description, argv, _fix in checks)


_CORE_TREE_SECTION_HEADER = "FuseSoC .core checks"


def _core_tree_project(root: Path) -> Path:
    """A Project whose ``.core`` tree trips every moved rule once."""
    booley_dir = _write_project(root)
    # Owned (a Doctor-selected sim Target) and schema-invalid: one schema FAIL.
    (root / "design.core").write_text(
        "CAPI=2:\n"
        "name: ::bad_depend:0\n"
        "filesets:\n"
        "  tb:\n"
        "    files: [tb/tb.sv: {file_type: systemVerilogSource}]\n"
        "    depend: not_a_list\n"
        "targets:\n"
        "  sim_bad:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
        "    filesets: [tb]\n"
        "    toplevel: tb\n",
        encoding="utf-8",
        newline="\n",
    )
    # Schema-valid, so FuseSoC keeps it: an `expression` parameter is a hard
    # security FAIL and the generator script a Scope-dependent advisory.
    (root / "sec.core").write_text(
        "CAPI=2:\n"
        "name: ::sec:0\n"
        "filesets:\n"
        "  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}\n"
        "  tb: {files: [tb/tb.sv: {file_type: systemVerilogSource}], tags: [tb]}\n"
        "parameters:\n"
        "  width: {datatype: expression, paramtype: vlogparam}\n"
        "generators:\n"
        "  gen: {command: rtl/gen.py}\n"
        "targets:\n"
        "  sim_sec:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
        "    filesets: [rtl, tb]\n"
        "    toplevel: tb\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "rtl" / "gen.py").write_text("print('generated')\n", encoding="utf-8", newline="\n")
    # Vendored (no Doctor Target) with a provider block, at the root so its
    # relative path has no separator: a NOTE.
    (root / "upstream.core").write_text(
        "CAPI=2:\n"
        "name: ::upstream:0\n"
        "provider: {name: github, user: acme, repo: demo}\n"
        "targets:\n"
        "  default: {}\n",
        encoding="utf-8",
        newline="\n",
    )
    (booley_dir / "doctor-waivers.toml").write_text(
        'version = 1\n[[waiver]]\ncheck = "core.security-scope-advisory"\n'
        'subject = "sec.core:-"\nreason = "Accepted for this fixture"\npermanent = true\n',
        encoding="utf-8",
        newline="\n",
    )
    library = root / "lib00"
    library.mkdir()
    symlink_or_skip(library / "self", "..", target_is_directory=True)
    return booley_dir


def _core_tree_section(output: str, root: Path) -> str:
    """The ``.core`` checks section with machine-specific paths normalized."""
    lines = output.splitlines()
    start = lines.index(_CORE_TREE_SECTION_HEADER)
    end = next((i for i in range(start, len(lines)) if not lines[i].strip()), len(lines))
    return "\n".join(lines[start:end]).replace(str(root.resolve()), "<root>").replace("\\", "/")


_CORE_TREE_SECTION = """\
FuseSoC .core checks
  ·  vendored .core upstream.core has a provider block, but no Doctor Target selects it
  ✗  FuseSoC recursive symlinks: 1 link resolves to ancestor <root> (lib00/self)
     → add a FUSESOC_IGNORE marker in each link's containing directory or an enclosing subtree (excludes all cores there); ensure markers are present in worktrees too, or remove the links
    recursive symlink lib00/self: directory link resolves to ancestor <root>
  ✗  .core schema design.core: filesets.tb.depend must be array
     → fix the .core — FuseSoC's CAPI2 schema requires this (caught here so it does not only surface under --deep)
  ✗  .core schema sec.core: parameters.width.datatype must be one of ['bool', 'file', 'int', 'real', 'str'] — FuseSoC ignores this whole .core, so none of its Targets resolve
     → fix the .core — FuseSoC's CAPI2 schema requires this (caught here so it does not only surface under --deep)
  ·  .core schema upstream.core: provider cannot be validated by any definition — FuseSoC ignores this whole .core, so none of its Targets resolve — vendored core, no Doctor Target selects it; FuseSoC skips it at resolve, so only that core's own Targets are unusable
  ✓  .core Targets enumerated: lint_fast, sim_bad, sim_fast, sim_sec, synth_fast
  ✓  Doctor Target matrix valid: 5 Target/Flow pair(s)
  ✓  .core Targets use the flow API (no legacy FuseSoC tools-section targets)
  ✓  Target names follow the <axis>_<subject> convention
  ✓  no dead 'default:' Targets in Booley-authored .core files
  ✓  synth Target 'synth_fast' (yosys) declares flow_options.arch
  ✓  3 Verilator sim Target(s) trace-capable (own a --exe main)
  ✓  sim Target 'sim_fast' TB fileset tagged
  ✓  sim Target 'sim_fast' TB emits a recognized pass sentinel
  ✓  lint Target 'lint_fast' toplevel 'dut' elaborates standalone
  ✓  generic Target 'synth_fast' toplevel 'dut' elaborates standalone
  !  2 .core-referenced file(s) exist on disk but are untracked by git (rtl/dut.sv, tb/tb.sv) — if gitignored (vendored firmware / build artifact), force-add with `git add -f <file>` and verify with `git ls-files <file>`; a fresh clone or CI checkout will lack them  project.core-file-untracked:rtl/dut.sv,tb/tb.sv
  ✗  .core security [expr_param] sec.core: parameter 'width' declares datatype 'expression', which is not a CAPI2 literal (bool/file/int/real/str). An HDL expression cannot be a faithful -G vlogparam and Booley ships no evaluator: resolve it to a literal, or convert it to a vlogdefine (decision 8).
     → ADR 0022 decision 21
  ~  .core security [in_scope_script] sec.core: generator 'gen' references 'rtl/gen.py', which is inside the agent's write Scope and therefore mutable. generators/hooks scripts must be out-of-Scope and read-only-mounted so the agent cannot author or edit them (decision 21). (advisory: doctor audits the union of writable category dirs, without a change-specific scope — the binding check runs at commit time)  core.security-scope-advisory:sec.core:-
     → reason: Accepted for this fixture
  ·  tests.toml not present (each sim Target runs its testbench's default test)
"""

_CORE_TREE_ROWS = [
    (
        Severity.NOTE,
        "vendored .core upstream.core has a provider block, but no Doctor Target selects it",
        "",
        None,
        None,
    ),
    (
        Severity.FAIL,
        "FuseSoC recursive symlinks: 1 link resolves to ancestor <root> (lib00/self)",
        "add a FUSESOC_IGNORE marker in each link's containing directory or an enclosing subtree (excludes all cores there); ensure markers are present in worktrees too, or remove the links",
        None,
        None,
    ),
    (
        Severity.FAIL,
        ".core schema design.core: filesets.tb.depend must be array",
        "fix the .core — FuseSoC's CAPI2 schema requires this (caught here so it does not only surface under --deep)",
        None,
        None,
    ),
    (
        Severity.FAIL,
        ".core schema sec.core: parameters.width.datatype must be one of ['bool', 'file', 'int', 'real', 'str'] — FuseSoC ignores this whole .core, so none of its Targets resolve",
        "fix the .core — FuseSoC's CAPI2 schema requires this (caught here so it does not only surface under --deep)",
        None,
        None,
    ),
    (
        Severity.NOTE,
        ".core schema upstream.core: provider cannot be validated by any definition — FuseSoC ignores this whole .core, so none of its Targets resolve — vendored core, no Doctor Target selects it; FuseSoC skips it at resolve, so only that core's own Targets are unusable",
        "",
        None,
        None,
    ),
    (
        Severity.FAIL,
        ".core security [expr_param] sec.core: parameter 'width' declares datatype 'expression', which is not a CAPI2 literal (bool/file/int/real/str). An HDL expression cannot be a faithful -G vlogparam and Booley ships no evaluator: resolve it to a literal, or convert it to a vlogdefine (decision 8).",
        "ADR 0022 decision 21",
        None,
        None,
    ),
    (
        Severity.WAIVED,
        ".core security [in_scope_script] sec.core: generator 'gen' references 'rtl/gen.py', which is inside the agent's write Scope and therefore mutable. generators/hooks scripts must be out-of-Scope and read-only-mounted so the agent cannot author or edit them (decision 21). (advisory: doctor audits the union of writable category dirs, without a change-specific scope — the binding check runs at commit time)",
        "Accepted for this fixture",
        "core.security-scope-advisory",
        "sec.core:-",
    ),
]


_CORE_TREE_ROW_PREFIXES = (
    ".core schema",
    ".core security",
    ".core tree has no",
    "FuseSoC recursive symlink",
    "vendored .core",
)


def _core_tree_rows(result, root: Path):
    """``(severity, message, fix, check_id, subject)`` of the moved rules' findings."""
    normalize = lambda text: text.replace(str(root.resolve()), "<root>").replace("\\", "/")  # noqa: E731
    return [
        (f.severity, normalize(f.message), normalize(f.fix), f.check_id, f.subject)
        for f in result.findings
        if f.message.startswith(_CORE_TREE_ROW_PREFIXES)
    ]


def test_core_tree_section_output_is_pinned(tmp_path, monkeypatch, capsys):
    """The .core checks section keeps its text, order, check IDs and exit code.

    The literals were captured from Doctor's own core-tree checks before they
    moved to their owner and are unchanged by the move; the rows now reach the
    section as the owner's typed reports.
    """
    booley_dir = _core_tree_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    reports = []
    render = doctor._Reporter.diagnostics

    def record_then_render(self, report):
        reports.append(report)
        render(self, report)

    monkeypatch.setattr(doctor._Reporter, "diagnostics", record_then_render)
    result = doctor.run_doctor_result(
        argparse.Namespace(deep=False, verbose=True, concise=False), tmp_path
    )
    section = _core_tree_section(capsys.readouterr().out, tmp_path)
    assert section == _CORE_TREE_SECTION.rstrip("\n")
    assert _core_tree_rows(result, tmp_path) == _CORE_TREE_ROWS
    assert result.exit_code == 1
    owner_rows = [
        finding.message.replace(str(tmp_path.resolve()), "<root>").replace("\\", "/")
        for report in reports
        for finding in report.findings
        if finding.message.startswith(_CORE_TREE_ROW_PREFIXES)
    ]
    assert owner_rows == [row[1] for row in _CORE_TREE_ROWS], (
        "the core-tree rows must reach Doctor as owner reports"
    )


def _core_tree_owner():
    """The ``.core`` tree owner module, imported where used."""
    return importlib.import_module("booley.audit.core_tree_inspection")


def test_core_audit_renders_core_tree_owner_reports_in_place(tmp_path, monkeypatch):
    """Doctor builds each owner request, renders each report once, and adds no rows of its own.

    The tree report (setup hazards, then schema) leads the section; the security
    report follows the Target checks, immediately before the Test Run plan report.
    """
    owner = _core_tree_owner()
    project = _cocotb_audit_project(tmp_path)
    tree_report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "tree-row"),))
    security_report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "security-row"),))
    tree_requests, security_requests = [], []

    def tree_spy(request):
        tree_requests.append(request)
        return tree_report

    def security_spy(request):
        security_requests.append(request)
        return security_report

    monkeypatch.setattr(owner, "inspect_core_tree", tree_spy)
    monkeypatch.setattr(owner, "inspect_core_security", security_spy)
    rendered = []
    rec = _Rec()
    doctor._run_core_audit(
        project, rec.p, rec.w, rec.s, rec.f, _note=rec.n, _diagnostics=rendered.append
    )

    seeds = doctor._project_target_matrix(project).seed_targets
    assert seeds
    assert tree_requests == [owner.CoreTreeInspectionRequest(tmp_path, seeds)]
    assert security_requests == [
        owner.CoreSecurityInspectionRequest(tmp_path, tuple(doctor._project_write_scope(tmp_path)))
    ]
    assert rendered[0] is tree_report
    assert rendered[-3] is security_report
    assert [item for item in rendered if item in (tree_report, security_report)] == [
        tree_report,
        security_report,
    ]
    moved_row_prefixes = (
        ".core schema",
        ".core security",
        ".core tree has no",
        "FuseSoC recursive symlink",
        "vendored .core",
    )
    assert not [m for _level, m in rec.events if m.startswith(moved_row_prefixes)]


_CORE_HYGIENE_ROW_PREFIXES = (
    "1 .core-referenced file(s) exist on disk",
    "1 $readmemh/$readmemb memory image(s)",
    "1 committed artifact(s) look built",
)


def _core_hygiene_owner():
    """The core repository hygiene owner module, imported where used."""
    return importlib.import_module("booley.runtime.core_repository_hygiene")


def _core_hygiene_project(root: Path) -> Path:
    """A real Git Project that trips each hygiene rule once.

    ``tb/tb.sv`` (a ``.core``-referenced file) and ``fw/boot.vmem`` (its
    ``$readmemh`` target, listed in no ``.core``) are untracked; the committed
    ``firmware/firmware.hex`` sits next to its ``hello.c`` source.
    """
    run_bounded_child(["git", "init", "-q"], cwd=root, check=True)
    booley_dir = _write_project(root)
    core = root / "unit.core"
    text = core.read_text(encoding="utf-8")
    text = text.replace(
        "targets:\n",
        "  fw:\n    files:\n      - firmware/firmware.hex: {file_type: user}\ntargets:\n",
        1,
    ).replace("    filesets: [rtl, tb]\n", "    filesets: [rtl, tb, fw]\n", 1)
    core.write_text(text, encoding="utf-8", newline="\n")
    (root / "tb" / "tb.sv").write_text(
        'module tb; reg [7:0] mem[0:255];\n  initial $display("[SIM_RESULT] PASSED");\n'
        '  initial $readmemh("fw/boot.vmem", mem);\nendmodule\n',
        encoding="utf-8",
        newline="\n",
    )
    (root / "fw").mkdir()
    (root / "fw" / "boot.vmem").write_text("00\n", encoding="utf-8", newline="\n")
    (root / "firmware").mkdir()
    (root / "firmware" / "firmware.hex").write_text("00\n", encoding="utf-8", newline="\n")
    (root / "firmware" / "hello.c").write_text("int main(){}\n", encoding="utf-8", newline="\n")
    run_bounded_child(
        [
            "git",
            "add",
            "-f",
            "unit.core",
            "rtl/dut.sv",
            "firmware/firmware.hex",
            "firmware/hello.c",
        ],
        cwd=root,
        check=True,
    )
    # The index is exactly the tracked set the pin below relies on.
    index = run_bounded_child(["git", "ls-files"], cwd=root, check=True).stdout.split()
    assert sorted(index) == [
        "firmware/firmware.hex",
        "firmware/hello.c",
        "rtl/dut.sv",
        "unit.core",
    ]
    return booley_dir


_CORE_HYGIENE_SECTION = """\
FuseSoC .core checks
  ✓  .core tree has no provider or recursive-symlink setup hazards
  ✓  .core CAPI2 schema valid
  ✓  .core Targets enumerated: lint_fast, sim_fast, synth_fast
  ✓  Doctor Target matrix valid: 3 Target/Flow pair(s)
  ✓  .core Targets use the flow API (no legacy FuseSoC tools-section targets)
  ✓  Target names follow the <axis>_<subject> convention
  ✓  no dead 'default:' Targets in Booley-authored .core files
  ✓  synth Target 'synth_fast' (yosys) declares flow_options.arch
  ✓  1 Verilator sim Target(s) trace-capable (own a --exe main)
  ✓  sim Target 'sim_fast' TB fileset tagged
  ✓  sim Target 'sim_fast' TB emits a recognized pass sentinel
  ✓  lint Target 'lint_fast' toplevel 'dut' elaborates standalone
  ✓  generic Target 'synth_fast' toplevel 'dut' elaborates standalone
  !  1 .core-referenced file(s) exist on disk but are untracked by git (tb/tb.sv) — if gitignored (vendored firmware / build artifact), force-add with `git add -f <file>` and verify with `git ls-files <file>`; a fresh clone or CI checkout will lack them  project.core-file-untracked:tb/tb.sv
  !  1 $readmemh/$readmemb memory image(s) exist on disk but are untracked by git (fw/boot.vmem) — if gitignored (vendored firmware / build output), force-add with `git add -f <file>`; a fresh clone or CI checkout will lack them and the sim boots uninitialized RAM  project.memory-image-untracked:fw/boot.vmem
  !  1 committed artifact(s) look built from in-repo source (firmware/firmware.hex) — ship the toolchain in the sandbox image and rebuild on demand (project Dockerfile + post-setup hook) instead of committing a frozen blob (booley-setup SKILL.md)  project.committed-build-artifact:firmware/firmware.hex
  ✓  .core security validation passed (no fpga hooks / expr-params / in-scope scripts)
  ·  tests.toml not present (each sim Target runs its testbench's default test)
"""

_CORE_HYGIENE_ROWS = [
    (
        Severity.WARN,
        "1 .core-referenced file(s) exist on disk but are untracked by git (tb/tb.sv) — if gitignored (vendored firmware / build artifact), force-add with `git add -f <file>` and verify with `git ls-files <file>`; a fresh clone or CI checkout will lack them",
        "",
        "project.core-file-untracked",
        "tb/tb.sv",
    ),
    (
        Severity.WARN,
        "1 $readmemh/$readmemb memory image(s) exist on disk but are untracked by git (fw/boot.vmem) — if gitignored (vendored firmware / build output), force-add with `git add -f <file>`; a fresh clone or CI checkout will lack them and the sim boots uninitialized RAM",
        "",
        "project.memory-image-untracked",
        "fw/boot.vmem",
    ),
    (
        Severity.WARN,
        "1 committed artifact(s) look built from in-repo source (firmware/firmware.hex) — ship the toolchain in the sandbox image and rebuild on demand (project Dockerfile + post-setup hook) instead of committing a frozen blob (booley-setup SKILL.md)",
        "",
        "project.committed-build-artifact",
        "firmware/firmware.hex",
    ),
]


def test_core_repository_hygiene_section_output_is_pinned(
    tmp_path, monkeypatch, capsys, isolated_git_attributes
):
    """The hygiene lines keep their text, order, check IDs, position and exit code.

    The literals were captured from Doctor's own hygiene checks before they moved
    to their owner and are unchanged by the move; the rows now reach the section
    as the owner's typed report. Only the owner's Git queries reach real Git; the
    fixture's fake answers every other Doctor command.
    """
    real_run = subprocess.run
    booley_dir = _core_hygiene_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    monkeypatch.setattr(
        _core_hygiene_owner(),
        "subprocess",
        SimpleNamespace(run=real_run, SubprocessError=subprocess.SubprocessError),
    )
    reports = []
    render = doctor._Reporter.diagnostics

    def record_then_render(self, report):
        reports.append(report)
        render(self, report)

    monkeypatch.setattr(doctor._Reporter, "diagnostics", record_then_render)
    result = doctor.run_doctor_result(
        argparse.Namespace(deep=False, verbose=True, concise=False), tmp_path
    )
    section = _core_tree_section(capsys.readouterr().out, tmp_path)
    assert section == _CORE_HYGIENE_SECTION.rstrip("\n")
    rows = [
        (f.severity, f.message, f.fix, f.check_id, f.subject)
        for f in result.findings
        if f.message.startswith(_CORE_HYGIENE_ROW_PREFIXES)
    ]
    assert rows == _CORE_HYGIENE_ROWS
    assert result.exit_code == 0
    assert [
        f.severity for f in result.findings if f.message.startswith(_CORE_HYGIENE_ROW_PREFIXES)
    ] == [Severity.WARN] * 3
    owner_rows = [
        finding.message
        for report in reports
        for finding in report.findings
        if finding.message.startswith(_CORE_HYGIENE_ROW_PREFIXES)
    ]
    assert owner_rows == [row[1] for row in _CORE_HYGIENE_ROWS], (
        "the hygiene rows must reach Doctor as one owner report"
    )


def test_core_audit_renders_core_repository_hygiene_report_in_place(tmp_path, monkeypatch):
    """Doctor builds the owner request, renders its report once, and adds no rows itself.

    The report follows the Target checks and leads the security report.
    """
    owner = _core_hygiene_owner()
    project = _cocotb_audit_project(tmp_path)
    hygiene_report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "hygiene-row"),))
    security_report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "security-row"),))
    hygiene_requests = []

    def hygiene_spy(request):
        hygiene_requests.append(request)
        return hygiene_report

    monkeypatch.setattr(owner, "inspect_core_repository_hygiene", hygiene_spy)
    monkeypatch.setattr(
        importlib.import_module("booley.audit.core_tree_inspection"),
        "inspect_core_security",
        lambda _request: security_report,
    )
    rendered = []
    rec = _Rec()
    doctor._run_core_audit(
        project, rec.p, rec.w, rec.s, rec.f, _note=rec.n, _diagnostics=rendered.append
    )

    assert hygiene_requests == [owner.CoreRepositoryHygieneRequest(tmp_path)]
    assert rendered.count(hygiene_report) == 1
    assert rendered.index(hygiene_report) + 1 == rendered.index(security_report)
    assert not [m for _level, m in rec.events if m.startswith(_CORE_HYGIENE_ROW_PREFIXES)]


def test_core_audit_skips_core_repository_hygiene_without_targets(tmp_path, monkeypatch):
    """No enumerated Target, no hygiene report: with no ``.core`` at all, and when
    Target enumeration raises."""
    owner = _core_hygiene_owner()
    calls = []
    monkeypatch.setattr(
        owner,
        "inspect_core_repository_hygiene",
        lambda request: calls.append(request) or DiagnosticReport(),
    )
    for case in ("no-core", "enumeration-error"):
        root = tmp_path / case
        root.mkdir()
        project = _cocotb_audit_project(root)
        if case == "no-core":
            (root / "design.core").unlink()
        with monkeypatch.context() as patch:
            if case == "enumeration-error":

                def refuse(_catalog, **_kwargs):
                    raise FuseSocError("catalog unavailable")

                patch.setattr(TargetCatalog, "list", refuse)
            rec = _Rec()
            doctor._run_core_audit(
                project, rec.p, rec.w, rec.s, rec.f, _note=rec.n, _diagnostics=lambda _r: None
            )
        failure = "project has no .core" if case == "no-core" else ".core enumeration failed"
        assert any(failure in message for level, message in rec.events if level == "fail"), case
        assert not calls, case


_CORE_RESOLUTION_OUTPUT = (
    "  ✗  .core Target 'sim_bad' fails to resolve\n"
    "     → fix the .core / depends graph\n"
    "      Parse error. Ignoring file /work/unit.core:\n"
    "      filesets.tb.depend must be array\n"
    "      second line\n"
    "  ✓  .core Target 'sim_ok' resolves\n"
)


def test_core_resolution_deep_output_is_pinned(tmp_path, monkeypatch, capsys):
    """The deep ``.core`` resolution rows keep their text, order, excerpt and counts.

    The literal was captured from Doctor's own resolution before it moved to its
    owner and is unchanged by the move; the rows now reach the reporter as the
    owner's one typed report, and the excerpt is shown without ``--verbose``.
    """
    from tests.harness.test_doctor_deep_execution import _core_resolution_project

    owner = importlib.import_module("booley.runtime.core_resolution")
    project = _core_resolution_project(tmp_path)
    monkeypatch.setattr(doctor.runtime_context, "inside_session_runtime", lambda: True)
    err = "Parse error. Ignoring file /work/unit.core:\nfilesets.tb.depend must be array\nsecond line"

    def run(argv, **_kwargs):
        verdicts = [
            {"selector": item["selector"], "ok": item["selector"] == "sim_ok", "err": err}
            for item in json.loads(argv[-1])
        ]
        return subprocess.CompletedProcess(
            argv, 0, "[[CORE_RESOLVE_JSON]]" + json.dumps(verdicts), ""
        )

    monkeypatch.setattr(doctor.subprocess, "run", run)
    results = []
    real_inspect = owner.inspect_core_resolution

    def spy(request):
        results.append(real_inspect(request))
        return results[-1]

    monkeypatch.setattr(owner, "inspect_core_resolution", spy)
    reporter = doctor._Reporter.create()

    doctor._run_core_resolution(
        project,
        doctor._DoctorFlowRuntime(tmp_path, None),
        reporter,
        doctor_deep.DeepCheckTracker(),
    )

    assert capsys.readouterr().out == _CORE_RESOLUTION_OUTPUT
    assert len(results) == 1, "the rows must reach Doctor as one owner report"
    assert (reporter.counts["pass"], reporter.counts["fail"]) == (1, 1)
    assert not reporter.result(1).clean
