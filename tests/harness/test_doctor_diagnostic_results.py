"""Composition keeps report policy and effect ordering at Doctor's public entry."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.audit import host_environment
from booley.audit.diagnostic_results import Severity
from booley.config.host_config import SandboxHostPolicy
from booley.core.host_shell import quote_arg
from booley.harness import auto_doctor, doctor, host_diagnostics
from booley.presentation import colors
from booley.runtime import (
    doctor_stamp,
    platform_paths,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import StaleTargetCatalogError
from tests.conftest import symlink_or_skip
from tests.diagnostic_helpers import _Rec
from tests.docker_cli_guard import (
    route_bounded_captures_through_subprocess_run,
    route_docker_helper_through_subprocess_run,
)
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.harness.doctor_host_support import answer_doctor_commands, hide_container_cli
from tests.harness.doctor_run_support import run_doctor, write_project
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
    # init_cmd binds reconcile_bootstrap at import; a first import under the patch below would
    # keep the stub for every later test in the process.
    importlib.import_module("booley.harness.init_cmd")
    inspect_host = host_diagnostics.inspect_host
    calls = patch_doctor_environment(monkeypatch, root, booley_dir)
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
    booley_dir = write_project(root)
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
    route_bounded_captures_through_subprocess_run(monkeypatch)
    return run_doctor(root, record_clean=True), inventory_calls


@pytest.mark.parametrize("egress", ["proxy", "unproxied"])
def test_host_doctor_reports_host_egress_mode(egress, tmp_path, monkeypatch):
    result, inventory_calls = _run_host_doctor(monkeypatch, tmp_path, egress=egress, sandboxes={})
    passed = [f.message for f in result.findings if f.severity == "pass"]
    assert f"host Sandbox egress mode: {egress}" in passed
    assert not [f for f in result.findings if f.check_id == "host.sandbox-egress-mismatch"]
    assert f"0 running Booley Sandbox(es) match host egress mode {egress}" in passed
    assert inventory_calls == [_INVENTORY_ARGV]


_MISMATCH_CASES = [
    # (host egress, editor Sandbox, stale Sandbox egress, recreate commands)
    ("unproxied", False, None, (["booley", "session", "refresh", "--project"],)),
    ("proxy", True, "unproxied", (["booley", "init", "--project"],)),
]


def test_host_doctor_reports_each_mismatched_running_sandbox(tmp_path, monkeypatch):
    """Another Project's mismatched Sandbox is a NOTE; this Project's own stays a WARN."""
    for number, (owner, case) in enumerate(
        (owner, case) for owner in ("other", "this") for case in _MISMATCH_CASES
    ):
        host, editor, stale, fix_commands = case
        doctor_root = tmp_path / f"doctor-{number}"
        doctor_root.mkdir()
        stale_root = doctor_root if owner == "this" else tmp_path / f"stale-{number}"
        stale_root.mkdir(exist_ok=True)
        current_root = tmp_path / f"current-{number}"
        current_egress = "unproxied" if host == "unproxied" else None
        sandboxes = {
            "stale-sandbox": _sandbox_document(
                "stale-sandbox", stale_root, editor=editor, egress=stale
            ),
            "current-sandbox": _sandbox_document(
                "current-sandbox", current_root, editor=not editor, egress=current_egress
            ),
        }
        with monkeypatch.context() as case_patch:
            result, inventory_calls = _run_host_doctor(
                case_patch, doctor_root, egress=host, sandboxes=sandboxes
            )
        sandbox_mode = "proxy" if stale is None else stale
        message = (
            f"Sandbox stale-sandbox (Project {stale_root.resolve()}) runs with egress "
            f"'{sandbox_mode}' but host [sandbox] egress = '{host}'"
        )
        mismatches = [f for f in result.findings if "stale-sandbox" in f.message]
        assert len(mismatches) == 1, result.findings
        finding = mismatches[0]
        assert finding.message == message
        if owner == "this":
            assert (finding.severity, finding.check_id) == ("warn", "host.sandbox-egress-mismatch")
            assert finding.subject == str(stale_root.resolve())
        else:
            assert (finding.severity, finding.check_id) == ("note", None)
            assert not [f for f in result.findings if f.check_id == "host.sandbox-egress-mismatch"]
        # Each recreate command names the stale Project, quoted for the host shell
        # (POSIX shells or PowerShell on Windows).
        for command in fix_commands:
            words = " ".join(command)
            assert f"`{words} {quote_arg(str(stale_root.resolve()))}`" in finding.fix
        assert ("Rebuild Container" in finding.fix) is editor
        assert "--seed" not in finding.fix
        assert inventory_calls == [_INVENTORY_ARGV]


def test_host_doctor_notes_orphan_stamps_until_prune_stamps(tmp_path, monkeypatch, capsys):
    """Orphan stamps of deleted Projects are a NOTE, and the stamp-only remedy clears it."""
    from booley.projects import cli as projects_cli
    from booley.projects import inventory
    from booley.runtime import image_keepers, session_admission, session_issuance

    xdg_home = tmp_path / "config"
    xdg_home.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    orphans = [str(tmp_path / "gone-a"), str(tmp_path / "gone-b")]
    for root in orphans:
        document = json.dumps({"project_root": root})
        session_issuance.stamp_store().write(root, document)
        session_admission.claim_store().write(root, document)
    for name in ("project-a", "project-b"):
        (tmp_path / name).mkdir()
    first, _ = _run_host_doctor(monkeypatch, tmp_path / "project-a", egress="proxy", sandboxes={})

    notes = [f for f in first.findings if "belong to deleted Projects" in f.message]
    assert len(notes) == 1, first.findings
    assert notes[0].severity == "note"
    command = " ".join(quote_arg(root) for root in orphans)
    assert f"booley projects prune-stamps {command}" in notes[0].fix
    first_active = [
        (f.severity, f.message) for f in first.findings if f.severity in ("fail", "warn")
    ]

    parser = argparse.ArgumentParser()
    projects_cli.add_subparser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["projects", "prune-stamps", *orphans])
    assert projects_cli.run(args, keeper_operations=image_keepers, leftovers_view=str) == 0
    capsys.readouterr()
    assert inventory.known_roots() == frozenset()

    second, _ = _run_host_doctor(monkeypatch, tmp_path / "project-b", egress="proxy", sandboxes={})

    assert not [f for f in second.findings if "belong to deleted Projects" in f.message]
    second_active = [
        (f.severity, f.message) for f in second.findings if f.severity in ("fail", "warn")
    ]
    assert second_active == first_active


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
    booley_dir = write_project(tmp_path)
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
    result = run_doctor(tmp_path, record_clean=True)
    finding = next(f for f in result.findings if f.check_id == "host.bootstrap-pending")
    assert (finding.severity, finding.subject) == ("waived" if waived else "warn", "skills")
    assert result.exit_code == 0
    assert result.clean is waived
    assert doctor_stamp.stamp_path(booley_dir).exists() is waived


_OBSERVATIONS = [
    ("config", lambda f: f.message in {"booley.toml parses", "booley.toml missing"}),
    ("upgrade", lambda f: f.message.startswith("Booley release review current")),
    ("guidance", lambda f: f.check_id == "guidance.links-unhealthy"),
    ("project-data-branch", lambda f: f.message.startswith("Booley Directory branch alignment")),
    ("prune", lambda f: "gc.worktreePruneExpire" in f.message),
    ("line-endings", lambda f: "working tree is container-safe" in f.message),
    ("shadow", lambda f: f.check_id == "fusesoc.worktree-core-shadow"),
    ("projections", lambda f: f.message == "stealth core projections match .booley/cores/"),
]


@pytest.mark.parametrize("valid", [False, True])
def test_project_observations_keep_the_existing_order(tmp_path, monkeypatch, valid):
    booley_dir = write_project(tmp_path)
    (booley_dir / "AGENTS.md").write_text("# Project guidance\n")
    with (booley_dir / "booley.toml").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n[stealth]\nenabled = true\n")
    if not valid:
        (booley_dir / "booley.toml").unlink()
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    result = run_doctor(tmp_path, record_clean=True)

    first_rows = {}
    for name, matches in _OBSERVATIONS:
        index = next((i for i, f in enumerate(result.findings) if matches(f)), None)
        if index is not None:
            first_rows[name] = index
    expected = [name for name, _matches in _OBSERVATIONS]
    if not valid:
        expected = ["config", "upgrade", "prune", "line-endings"]
    assert sorted(first_rows, key=first_rows.__getitem__) == expected
    assert not (tmp_path / "AGENTS.md").exists()


# Doctor's own evidence and scratch output stay writable; nothing else may change.
_DOCTOR_WRITABLE_PREFIXES = (
    ".git/",
    ".booley/runtime/",
    ".booley/tmp/",
)


def _project_files(root: Path) -> dict[str, bytes]:
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    return {
        name: data
        for name, data in files.items()
        if not name.startswith(_DOCTOR_WRITABLE_PREFIXES)
    }


@pytest.mark.parametrize("deep", [False, True])
def test_manual_and_automatic_doctor_agree_and_write_no_project_file(tmp_path, monkeypatch, deep):
    booley_dir = write_project(tmp_path)
    (booley_dir / "AGENTS.md").write_text("# Project guidance\n")
    with (booley_dir / "booley.toml").open("a") as stream:
        stream.write("\n[stealth]\nenabled = true\n")
    (booley_dir / "cores").mkdir()
    (booley_dir / "cores" / "hidden.core").write_text("CAPI=2:\nname: ::hidden:0\n")
    run_bounded_child(["git", "init", "-q", str(tmp_path)], check=True)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    before = _project_files(tmp_path)

    automatic = auto_doctor.run_if_due(tmp_path, trigger="test")
    assert _project_files(tmp_path) == before
    manual = run_doctor(tmp_path, deep=deep, record_clean=True)
    assert _project_files(tmp_path) == before

    assert automatic is not None
    graded = [f for f in manual.findings if f.severity in {"warn", "fail"}]
    links = next(f for f in graded if f.check_id == "guidance.links-unhealthy")
    stale = next((f for f in graded if f.check_id == "stealth.projections-not-current"), None)
    assert stale is not None
    assert "booley init" in links.fix
    assert "booley init" in stale.fix
    assert [
        (f["severity"], f["check_id"], f["message"], f["fix"])
        for f in automatic["findings"]
        if f["severity"] in {"warn", "fail"}
    ] == [(f.severity, f.check_id, f.message, f.fix) for f in graded]
    assert {k: automatic["counts"][k] for k in ("warn", "fail")} == {
        k: manual.counts[k] for k in ("warn", "fail")
    }
    messages = [f.message for f in manual.findings]
    skip = "skipped - stealth core projections are not current"
    assert f"Flow dry-runs {skip}" in messages
    assert f"Flow dry-runs {skip}" in [f["message"] for f in automatic["findings"]]
    if deep:
        assert f"deep checks {skip}" in messages
        assert manual.deep_status is not None
        assert not manual.deep_status.current


_COCOTB_CORE = """\
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


@pytest.mark.parametrize("cocotb", [False, True])
def test_doctor_reports_cocotb_targets(tmp_path, monkeypatch, cocotb):
    """The cocotb rows keep their text, severity, IDs, subjects, order, waiver and exit code.

    ``sim_a`` has a tagged module that defines only ``test_reset`` yet a qualified
    tests.toml section names ``test_typo`` too and declares ``select``; ``sim_b`` sits
    in an untagged fileset and its module is not on disk. Without a cocotb Target Doctor
    neither checks the container for cocotb nor reports a cocotb row.
    """
    booley_dir = write_project(tmp_path)
    if cocotb:
        (tmp_path / "demo.core").write_text(_COCOTB_CORE, encoding="utf-8", newline="\n")
        (tmp_path / "tb" / "test_a.py").write_text(
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
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    result = run_doctor(tmp_path, record_clean=True)

    rows = [
        (f.severity, f.message, f.fix, f.check_id, f.subject)
        for f in result.findings
        if f.message.startswith("cocotb")
    ]
    if not cocotb:
        assert rows == []
        return
    assert rows == [
        (Severity.PASS, "cocotb in container (a Cocotb Target exists)", "", None, None),
        (
            Severity.FAIL,
            "cocotb Target 'sim_a' declares a tests.toml `select` plusarg template",
            "remove `select` — cocotb test selection is the COCOTB_TEST_FILTER env var "
            "built from the `tests` list",
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
    assert result.counts["waived"] == 1
    assert result.exit_code == 1


_CORE_TREE_SECTION_HEADER = "FuseSoC .core checks"


def _core_tree_project(root: Path) -> Path:
    """A Project whose ``.core`` tree trips every moved rule once."""
    booley_dir = write_project(root)
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
    # An `expression` parameter is a hard
    # security FAIL and a generator script a Scope-dependent advisory; `hw/` is a nonstandard
    # directory only the `.core` fileset names, so a Scope hardcoded to `rtl/`, `tb/` misses it.
    (root / "sec.core").write_text(
        "CAPI=2:\n"
        "name: ::sec:0\n"
        "filesets:\n"
        "  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}\n"
        "  tb: {files: [tb/tb.sv: {file_type: systemVerilogSource}], tags: [tb]}\n"
        "  hw: {files: [hw/top.sv: {file_type: systemVerilogSource}]}\n"
        "parameters:\n"
        "  width: {datatype: expression, paramtype: vlogparam}\n"
        "generators:\n"
        "  gen: {command: rtl/gen.py}\n"
        "  hw_gen: {command: hw/gen.py}\n"
        "  fw_gen: {command: fw/gen.py}\n"
        "targets:\n"
        "  sim_sec:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
        "    filesets: [rtl, tb, hw]\n"
        "    toplevel: tb\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "hw").mkdir()
    (root / "hw" / "gen.py").write_text("print('generated')\n", encoding="utf-8", newline="\n")
    (root / "rtl" / "gen.py").write_text("print('generated')\n", encoding="utf-8", newline="\n")
    # No `.core` fileset names `fw/`, so it is outside the write Scope and its generator is
    # no advisory: a hardcoded `fw/*` in Doctor's Scope would add one (#4052).
    (root / "fw").mkdir()
    (root / "fw" / "gen.py").write_text("print('generated')\n", encoding="utf-8", newline="\n")
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
  ✓  synth Target 'synth_fast' (yosys) declares flow_options.arch
  ✓  3 Verilator sim Target(s) trace-capable (own a --exe main)
  ✓  sim Target 'sim_fast' TB fileset tagged
  ✓  sim Target 'sim_fast' TB emits a recognized pass sentinel
  ✓  lint Target 'lint_fast' toplevel 'dut' elaborates standalone
  ✓  generic Target 'synth_fast' toplevel 'dut' elaborates standalone
  !  2 .core-referenced file(s) exist on disk but are untracked by git (rtl/dut.sv, tb/tb.sv) — if gitignored (vendored firmware / build artifact), force-add with `git add -f <file>` and verify with `git ls-files <file>`; a fresh clone or CI checkout will lack them  project.core-file-untracked:rtl/dut.sv,tb/tb.sv
  ✗  .core security [expr_param] sec.core: parameter 'width' declares datatype 'expression', which is not a CAPI2 literal (bool/file/int/real/str). An HDL expression cannot be a faithful -G vlogparam and Booley ships no evaluator.
     → give parameter 'width' a literal datatype (bool, file, int, real or str) and the literal value as its default
  ~  .core security [in_scope_script] sec.core: generator 'gen' references 'rtl/gen.py', which is inside the agent's write Scope and therefore mutable. generator and hook scripts must sit outside the agent's write Scope, so they are not files the agent edits as part of its work. (advisory: doctor audits the union of writable category dirs, without a change-specific scope — the binding check runs at commit time)  core.security-scope-advisory:sec.core:-
     → reason: Accepted for this fixture
  ~  .core security [in_scope_script] sec.core: generator 'hw_gen' references 'hw/gen.py', which is inside the agent's write Scope and therefore mutable. generator and hook scripts must sit outside the agent's write Scope, so they are not files the agent edits as part of its work. (advisory: doctor audits the union of writable category dirs, without a change-specific scope — the binding check runs at commit time)  core.security-scope-advisory:sec.core:-
     → reason: Accepted for this fixture
  ·  tests.toml not present (each sim Target runs its testbench's default test)
"""


def test_core_tree_section_output_is_pinned(tmp_path, monkeypatch, capsys):
    """The .core checks section keeps its text, order, check IDs and exit code.

    The literals were captured from Doctor's own core-tree checks before they
    moved to their owner and are unchanged by the move.
    """
    booley_dir = _core_tree_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    result = run_doctor(tmp_path, verbose=True, record_clean=True)
    section = _core_tree_section(capsys.readouterr().out, tmp_path)
    assert section == _CORE_TREE_SECTION.rstrip("\n")
    assert result.exit_code == 1


def _core_hygiene_project(root: Path) -> Path:
    """A real Git Project that trips each hygiene rule once.

    ``tb/tb.sv`` (a ``.core``-referenced file) and ``fw/boot.vmem`` (its
    ``$readmemh`` target, listed in no ``.core``) are untracked; the committed
    ``firmware/firmware.hex`` sits next to its ``hello.c`` source.
    """
    run_bounded_child(["git", "init", "-q"], cwd=root, check=True)
    booley_dir = write_project(root)
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


def test_core_repository_hygiene_section_output_is_pinned(
    tmp_path, monkeypatch, capsys, isolated_git_attributes
):
    """The hygiene lines keep their text, order, check IDs, position and exit code.

    The literals were captured from Doctor's own hygiene checks before they moved
    to their owner and are unchanged by the move. Only the owner's Git queries reach
    real Git; the fixture's fake answers every other Doctor command.
    """
    real_run = subprocess.run
    real_bounded = platform_paths.run_captured_bounded
    booley_dir = _core_hygiene_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    owner = importlib.import_module("booley.runtime.core_repository_hygiene")
    monkeypatch.setattr(
        owner,
        "subprocess",
        SimpleNamespace(run=real_run, SubprocessError=subprocess.SubprocessError),
    )
    monkeypatch.setattr(
        owner, "platform_paths", SimpleNamespace(run_captured_bounded=real_bounded)
    )
    result = run_doctor(tmp_path, verbose=True, record_clean=True)
    section = _core_tree_section(capsys.readouterr().out, tmp_path)
    assert section == _CORE_HYGIENE_SECTION.rstrip("\n")
    assert result.exit_code == 0


@pytest.mark.parametrize("case", ["no-core", "enumeration-error"])
def test_core_audit_stops_when_targets_cannot_be_enumerated(tmp_path, monkeypatch, case):
    """With no enumerated Target the audit reports a FAIL and runs no Target check.

    That is a Project with no ``.core`` at all, and a Target listing that raises. Private:
    `_run_core_audit` is called directly so both cases share one stop assertion on its
    recorder; the no-core case alone could run through ``run_doctor_result``.
    """
    booley_dir = write_project(tmp_path)
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=booley_dir,
        booley_toml={},
        first_target="x",
    )
    if case == "no-core":
        (tmp_path / "unit.core").unlink()
    else:
        # Patched: no public entry point reaches this branch. A `.core` that makes
        # `TargetCatalog.build` raise crashes Doctor earlier, in the core-tree owner, and
        # `list` fails alone only when the Target inputs change between `build` and `list`.
        def stale(_catalog, **_kwargs):
            raise StaleTargetCatalogError("catalog unavailable")

        monkeypatch.setattr(TargetCatalog, "list", stale)
    rec = _Rec()
    reports = []

    doctor._run_core_audit(
        project, rec.p, rec.w, rec.s, rec.f, _note=rec.n, _diagnostics=reports.append
    )

    failure = "project has no .core" if case == "no-core" else ".core enumeration failed"
    assert any(failure in message for level, message in rec.events if level == "fail")
    if case == "enumeration-error":
        # Only the core-tree report was rendered before the audit stopped.
        assert len(reports) == 1


_CORE_RESOLUTION_BLOCK = [
    "  ✓  .core Target 'lint_fast' resolves",
    "  ✗  .core Target 'sim_fast' fails to resolve",
    "     → fix unit.core (declares Target 'sim_fast') or its depends graph",
    "      Parse error. Ignoring file /work/unit.core:",
    "      filesets.tb.depend must be array",
    "      second line",
    "  ✓  .core Target 'synth_fast' resolves",
]


def test_core_resolution_deep_output_is_pinned(tmp_path, monkeypatch, capsys):
    """The deep ``.core`` resolution rows keep their text, order and excerpt.

    The literal was captured from Doctor's own resolution before it moved to its
    owner; only the failing Target's fix changed (it names the ``.core``). The
    excerpt is shown without ``--verbose``.
    """
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    err = "Parse error. Ignoring file /work/unit.core:\nfilesets.tb.depend must be array\nsecond line"

    def sandbox_resolves_all_but_sim_fast(argv, _env):
        if "CORE_RESOLVE_JSON" not in " ".join(argv):
            return None
        verdicts = [
            {"selector": item["selector"], "ok": item["selector"] != "sim_fast", "err": err}
            for item in json.loads(argv[-1])
        ]
        return subprocess.CompletedProcess(
            argv, 0, "[[CORE_RESOLVE_JSON]]" + json.dumps(verdicts), ""
        )

    answer_doctor_commands(monkeypatch, sandbox_resolves_all_but_sim_fast)

    result = run_doctor(tmp_path, deep=True, record_clean=True)

    lines = colors.strip_ansi(capsys.readouterr().out).splitlines()
    start = lines.index(_CORE_RESOLUTION_BLOCK[0])
    assert lines[start : start + len(_CORE_RESOLUTION_BLOCK)] == _CORE_RESOLUTION_BLOCK
    assert [f.message for f in result.findings if f.message.endswith("fails to resolve")] == [
        ".core Target 'sim_fast' fails to resolve"
    ]
    assert not result.clean


_FPGA_CORE = (
    "CAPI=2:\nname: ::design:0\ntargets:\n  fpga_board:\n    flow: generic\n"
    "    flow_options: {tool: verilator, part: xc7a35tcsg324-1, booley: {doctor: [fpga]}}\n"
)
_BAKE_REMEDY = (
    "in VS Code, on the host run `booley init`, then use Dev Containers: Rebuild Container; "
    "headless, run `booley session refresh` on the host"
)
_RUNTIME_ROWS = {
    "binary missing": (
        "lint",
        1,
        [
            "  ✓  lint executes in the Sandbox",
            "  ✗  lint: 'verilator' is not on the issued Sandbox PATH",
            "     → bake verilator into the Sandbox Image, then recreate the Sandbox: "
            + _BAKE_REMEDY,
            "  ✓  lint dry-run [lint_fast] passed",
        ],
    ),
    "runtime unavailable": (
        "lint",
        0,
        [
            "  ✓  lint executes in the Sandbox",
            "  ·  lint: 'verilator' on the Sandbox PATH - container runtime unavailable",
            "  ·  lint dry-run [lint_fast] skipped - 'docker' runtime not available",
        ],
    ),
    "vivado inner timeout": (
        "fpga",
        1,
        [
            "  ✓  fpga executes in the Sandbox",
            "  ✗  fpga: Vivado batch startup: Vivado did not start within 27s",
            "     → verify Vivado runtime dependencies in the Sandbox Image",
            "  ✓  fpga dry-run [fpga_board] passed",
        ],
    ),
}


@pytest.mark.parametrize("scenario", _RUNTIME_ROWS)
def test_flow_runtime_rows_render_unchanged(tmp_path, monkeypatch, capsys, scenario):
    """The Flow runtime rows keep their text, position and exit code.

    Each scenario sits between the Flow's ``executes in the Sandbox``
    row and its dry-run row, and the exit code follows the severity.
    """
    flow, exit_code, rows = _RUNTIME_ROWS[scenario]
    booley_dir = write_project(tmp_path)
    if flow == "fpga":
        (tmp_path / "design.core").write_text(_FPGA_CORE, encoding="utf-8", newline="\n")
        with (booley_dir / "booley.toml").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n[flows.fpga]\n")
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    if scenario == "binary missing":
        answer_doctor_commands(
            monkeypatch,
            lambda argv, _env: (
                subprocess.CompletedProcess(argv, 1, "", "")
                if argv[-3:] == ["sh", "-c", "command -v verilator"]
                else None
            ),
        )
    elif scenario == "runtime unavailable":
        hide_container_cli(monkeypatch)
    else:
        answer_doctor_commands(
            monkeypatch,
            lambda argv, _env: (
                subprocess.CompletedProcess(argv, 124, "", "")
                if "BOOLEY_VIVADO_STARTUP_" in " ".join(argv)
                else None
            ),
        )

    result = run_doctor(tmp_path, record_clean=True)

    lines = colors.strip_ansi(capsys.readouterr().out).splitlines()
    start = lines.index(f"  ✓  {flow} executes in the Sandbox")
    end = next(i for i in range(start, len(lines)) if " dry-run" in lines[i])
    assert lines[start : end + 1] == rows
    assert result.exit_code == exit_code
