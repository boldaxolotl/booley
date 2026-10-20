"""Unknown attributes sources must not become policy or authorize publication."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from booley.audit.doctor_waivers import load_doctor_waivers
from booley.harness import doctor, init_cmd
from booley.harness.setup import git_hooks, line_endings
from booley.harness.setup.common import InitContext
from booley.harness.setup.line_endings import (
    LineEndingActionKind as ActionKind,
)
from booley.harness.setup.line_endings import (
    LineEndingActionState as ActionState,
)
from booley.harness.setup.line_endings import (
    LineEndingMode as Mode,
)
from booley.harness.setup.line_endings import (
    LineEndingObservation as Observation,
)
from booley.harness.setup.line_endings import (
    LineEndingObservationCode as Code,
)
from booley.harness.setup.line_endings import (
    LineEndingStatus as Status,
)
from booley.runtime.booley_dir import init_booley_dir_scope

pytestmark = pytest.mark.usefixtures("isolated_git_attributes")


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=10, check=True
    )
    return result.stdout


def _checkout(tmp_path: Path, *, default: bool = True):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.attributesFile", "")
    (root / ".gitignore").write_text(".booley/\n")
    _git(root, "add", ".gitignore")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "test")
    data = root / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text("[stealth]\nenabled = true\n")
    local = root / ".git/info/attributes"
    if default:
        local.write_bytes(b"* text=auto eol=lf\n")
    return root, data, local


def _missing_sibling(root: Path, tmp_path: Path, name: str = "sibling with spaces"):
    sibling = tmp_path / name
    _git(root, "worktree", "add", "-qb", name.replace(" ", "-"), str(sibling))
    parked = tmp_path / (name + " parked")
    sibling.rename(parked)
    return sibling, parked


def _snapshot(root: Path):
    paths = [root / ".gitignore", *[p for p in (root / ".git").rglob("*") if p.is_file()]]
    return {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in paths}


def _unknown(report):
    return [o for o in report.observations if o.code.value == "attributes-policy-unknown"]


def _fail_attributes_snapshot(monkeypatch):
    real_observe = line_endings._observe_attributes_snapshot

    def observe(target, error, observations):
        with monkeypatch.context() as probe:
            probe.setattr(
                line_endings, "_optional_file_identity", lambda _path: (None, "snapshot failed")
            )
            return real_observe(target, error, observations)

    monkeypatch.setattr(line_endings, "_observe_attributes_snapshot", observe)


@pytest.mark.parametrize("mode", list(Mode))
def test_unknown_sibling_preserves_default_without_false_conflict(tmp_path, mode):
    root, data, local = _checkout(tmp_path)
    sibling, parked = _missing_sibling(root, tmp_path)
    before = _snapshot(root)
    report = line_endings.reconcile_project_line_endings(root, data, mode=mode)
    checkout = report.repositories[0]
    assert Code.LOCAL_POLICY_CONFLICT not in {o.code for o in checkout.observations}
    assert _unknown(checkout) and str(sibling) in _unknown(checkout)[0].detail
    assert "no local default installed" not in _unknown(checkout)[0].detail
    assert Code.UPSTREAM_POLICY not in {o.code for o in checkout.observations}
    assert checkout.status is Status.SAFE
    assert checkout.actions == ()
    assert _snapshot(root) == before
    parked.rename(sibling)
    restored = line_endings.reconcile_project_line_endings(root, data, mode=mode)
    assert not _unknown(restored.repositories[0])
    assert local.read_bytes() == b"* text=auto eol=lf\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX filenames can contain non-UTF-8 bytes")
def test_unknown_sibling_bytes_can_be_printed_and_persisted_as_utf8(tmp_path):
    root, data, _local = _checkout(tmp_path)
    _missing_sibling(root, tmp_path, os.fsdecode(b"sibling-\xff"))
    reporter = _reporter(data)
    doctor._check_line_endings(
        root, reporter.pass_, reporter.warn_, reporter.skip_, reporter.fail_, booley_dir=data
    )
    message = reporter.findings[0].message
    message.encode("utf-8")
    assert "sibling-\\xff" in message
    with init_booley_dir_scope(root):
        assert doctor.record_manual_result(root, reporter.result(0)) is not None
    assert "sibling-\\xff" in (data / "runtime/doctor/last.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("autocrlf", ["false", "true", None])
def test_unknown_sibling_blocks_every_publication_trigger(tmp_path, mode, autocrlf):
    root, data, local = _checkout(tmp_path, default=False)
    sibling, parked = _missing_sibling(root, tmp_path)
    if autocrlf is None:
        _git(root, "config", "--unset", "core.autocrlf")
    else:
        _git(root, "config", "core.autocrlf", autocrlf)
    report = line_endings.reconcile_project_line_endings(root, data, mode=mode)
    checkout = report.repositories[0]
    assert _unknown(checkout)
    assert Code.LOCAL_POLICY_MISSING not in {o.code for o in checkout.observations}
    assert not any(a.kind is ActionKind.PUBLISH_ATTRIBUTES for a in checkout.actions)
    assert not local.exists()
    if mode is Mode.REPAIR:
        assert _git(root, "config", "--local", "core.autocrlf").strip() == b"false"
        assert report.status is Status.SAFE
    parked.rename(sibling)
    if mode is Mode.REPAIR:
        restored = line_endings.reconcile_project_line_endings(root, data, mode=mode)
        assert restored.status is Status.SAFE and local.exists()


@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("source", ["system", "nonregular-sibling"])
def test_other_unknown_sources_are_not_policy(tmp_path, monkeypatch, default, source):
    root, data, local = _checkout(tmp_path, default=default)
    if source == "system":  # older Git: legacy user fallback beside an unresolved system path
        monkeypatch.delenv("GIT_ATTR_NOSYSTEM")
        monkeypatch.setattr(line_endings, "_native_attribute_path", lambda *_: (False, None))
    else:
        sibling = tmp_path / "sibling"
        _git(root, "worktree", "add", "-qb", "sibling", str(sibling))
        _git(root, "config", "extensions.worktreeConfig", "true")
        selected = tmp_path / "nonregular.attributes"
        selected.mkdir()
        _git(sibling, "config", "--worktree", "core.attributesFile", str(selected))
    report = line_endings.reconcile_project_line_endings(root, data, mode=Mode.REPAIR)
    checkout = report.repositories[0]
    assert report.status is Status.SAFE
    assert _unknown(checkout)
    if source == "nonregular-sibling":
        assert "cannot inspect sibling worktree" in _unknown(checkout)[0].detail
        assert "type and access" in _unknown(checkout)[0].detail
    assert not {
        Code.LOCAL_POLICY_CONFLICT,
        Code.LOCAL_POLICY_MISSING,
        Code.UPSTREAM_POLICY,
    }.intersection(o.code for o in checkout.observations)
    assert local.exists() is default


@pytest.mark.parametrize("default", [False, True])
def test_unknown_system_policy_is_reported_once_across_worktrees(
    tmp_path, monkeypatch, capsys, default
):
    root, data, _local = _checkout(tmp_path, default=default)
    sibling = tmp_path / "accessible sibling"
    _git(root, "worktree", "add", "-qb", "sibling", str(sibling))
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM")
    real_native = line_endings._native_attribute_path

    def native(root, variable):
        return (False, None) if variable == "GIT_ATTR_SYSTEM" else real_native(root, variable)

    monkeypatch.setattr(line_endings, "_native_attribute_path", native)
    report = line_endings.reconcile_project_line_endings(root, data, mode=Mode.INSPECT)
    assert len(_unknown(report.repositories[0])) == 1
    reporter = _reporter(data)
    doctor._check_line_endings(
        root, reporter.pass_, reporter.warn_, reporter.skip_, reporter.fail_, booley_dir=data
    )
    assert reporter.findings[0].message.count("system attributes path unresolved") == 1
    assert reporter.finish() == 0 and not reporter.result(0).clean
    capsys.readouterr()
    ctx = InitContext(project_root=root, interactive=False)
    git_hooks._step_line_endings(ctx, data)
    assert capsys.readouterr().out.count("system attributes path unresolved") == 1
    assert ctx.results[-1].detail.count("system attributes path unresolved") == 1


@pytest.mark.parametrize("default", [False, True])
def test_unknown_sibling_does_not_hide_known_conflict(tmp_path, default):
    root, data, local = _checkout(tmp_path, default=default)
    _missing_sibling(root, tmp_path)
    (root / ".gitattributes").write_text("*.txt text eol=crlf\n")
    report = line_endings.reconcile_project_line_endings(root, data, mode=Mode.INSPECT)
    checkout = report.repositories[0]
    assert checkout.status is (Status.UNSAFE if default else Status.SAFE)
    assert (Code.LOCAL_POLICY_CONFLICT in {o.code for o in checkout.observations}) is default
    assert _unknown(checkout)
    assert local.exists() is default
    if default:
        assert local.read_bytes() == b"* text=auto eol=lf\n"


def test_unknown_sibling_does_not_publish_after_crlf_normalization(tmp_path):
    root, data, local = _checkout(tmp_path, default=False)
    _git(root, "config", "core.autocrlf", "true")
    tracked = root / "design.v"
    tracked.write_bytes(b"module design;\nendmodule\n")
    _git(root, "add", "design.v")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "RTL")
    tracked.unlink()
    _git(root, "checkout", "--", "design.v")
    assert b"\r\n" in tracked.read_bytes()
    _missing_sibling(root, tmp_path)
    report = line_endings.reconcile_project_line_endings(root, data, mode=Mode.REPAIR)
    assert report.status is Status.SAFE
    assert b"\r\n" not in tracked.read_bytes()
    assert any(a.kind is ActionKind.NORMALIZE_FILES for a in report.repositories[0].actions)
    assert not local.exists() and _unknown(report.repositories[0])


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("failure", ["policy", "snapshot"])
def test_unknown_never_erases_publication_error(tmp_path, monkeypatch, mode, failure):
    root, data, _local = _checkout(tmp_path, default=failure == "policy")
    if failure == "snapshot":
        _git(root, "config", "--unset", "core.autocrlf")
    _missing_sibling(root, tmp_path)
    if failure == "policy":

        def failed_policy(_root):
            raise ValueError("policy failed")

        monkeypatch.setattr(line_endings, "_crlf_index_dirt", failed_policy)
    else:
        _fail_attributes_snapshot(monkeypatch)
    report = line_endings.reconcile_project_line_endings(root, data, mode=mode)
    assert report.status is Status.UNSAFE
    assert _unknown(report.repositories[0])
    assert any(
        a.kind is ActionKind.PUBLISH_ATTRIBUTES and a.state is ActionState.REFUSED
        for a in report.repositories[0].actions
    )
    reporter = _reporter(data)
    doctor._report_repository_line_endings(
        report.repositories[0], reporter.pass_, reporter.warn_, reporter.fail_
    )
    primary = (
        "git.autocrlf-risk"
        if failure == "snapshot" and mode is Mode.INSPECT
        else "git.line-endings-unreadable"
    )
    assert {f.check_id for f in reporter.findings} == {"git.attributes-policy-unknown", primary}


@pytest.mark.parametrize("mode", list(Mode))
def test_unknown_snapshot_error_without_trigger_does_not_invent_publication(
    tmp_path, monkeypatch, mode
):
    root, data, _local = _checkout(tmp_path, default=False)
    _missing_sibling(root, tmp_path)
    _fail_attributes_snapshot(monkeypatch)
    report = line_endings.reconcile_project_line_endings(root, data, mode=mode)
    assert report.status is Status.UNSAFE
    assert not any(a.kind is ActionKind.PUBLISH_ATTRIBUTES for a in report.repositories[0].actions)


@pytest.mark.parametrize("mode", list(Mode))
def test_current_checkout_failure_is_not_a_sibling_limitation(tmp_path, monkeypatch, mode):
    root, data, local = _checkout(tmp_path)
    _missing_sibling(root, tmp_path)
    real_native = line_endings._native_attribute_path

    def native(checkout, variable):
        if checkout == root:
            raise ValueError("current checkout policy unavailable")
        return real_native(checkout, variable)

    monkeypatch.setattr(line_endings, "_native_attribute_path", native)
    report = line_endings.reconcile_project_line_endings(root, data, mode=mode)
    assert report.status is Status.UNSAFE
    checkout = report.repositories[0]
    assert not _unknown(checkout)
    assert Code.CANDIDATE_UNSAFE in {o.code for o in checkout.observations}
    assert local.read_bytes() == b"* text=auto eol=lf\n"


@pytest.mark.parametrize("default", [False, True])
def test_restored_sibling_real_policy_is_still_respected(tmp_path, default):
    root, data, local = _checkout(tmp_path, default=default)
    sibling = tmp_path / "sibling"
    _git(root, "worktree", "add", "-qb", "sibling", str(sibling))
    _git(root, "config", "extensions.worktreeConfig", "true")
    selected = sibling / "policy.attributes"
    selected.write_text("*.txt -text\n")
    _git(sibling, "config", "--worktree", "core.attributesFile", str(selected))
    parked = tmp_path / "parked"
    sibling.rename(parked)
    assert _unknown(
        line_endings.reconcile_project_line_endings(root, data, mode=Mode.INSPECT).repositories[0]
    )
    parked.rename(sibling)
    report = line_endings.reconcile_project_line_endings(root, data, mode=Mode.REPAIR)
    checkout = report.repositories[0]
    assert not _unknown(checkout)
    assert Code.UPSTREAM_POLICY in {o.code for o in checkout.observations}
    assert (Code.LOCAL_POLICY_CONFLICT in {o.code for o in checkout.observations}) is default
    assert local.exists() is default


def _reporter(data: Path):
    return doctor._Reporter.create(load_doctor_waivers(data))


def test_doctor_unknown_siblings_warn_read_only_in_stable_order(tmp_path):
    root, data, _local = _checkout(tmp_path)
    _missing_sibling(root, tmp_path, "z sibling")
    _missing_sibling(root, tmp_path, "a sibling")
    before = _snapshot(root)
    reporter = _reporter(data)
    doctor._check_line_endings(
        root, reporter.pass_, reporter.warn_, reporter.skip_, reporter.fail_, booley_dir=data
    )
    assert len(reporter.findings) == 1
    finding = reporter.findings[0]
    assert (
        finding.check_id == "git.attributes-policy-unknown"
        and finding.subject == "project-checkout"
    )
    assert finding.severity == "warn" and "remove" not in finding.fix
    assert "booley init" not in finding.fix
    assert finding.message.index("a sibling") < finding.message.index("z sibling")
    assert reporter.finish() == 0 and not reporter.result(0).clean
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "other",
    [
        "stale-index",
        "candidate-unsafe",
        "crlf-mismatch",
        "autocrlf-effective-true",
        "local-policy-conflict",
        "leaked-root-policy",
    ],
)
def test_doctor_unknown_does_not_mask_primary_findings(tmp_path, other):
    root, data, _local = _checkout(tmp_path)
    _missing_sibling(root, tmp_path)
    report = line_endings.reconcile_project_line_endings(
        root, data, mode=Mode.INSPECT
    ).repositories[0]
    report = replace(
        report,
        observations=(
            *report.observations,
            Observation(Code(other), count=1, detail="actual problem"),
        ),
    )
    reporter = _reporter(data)
    doctor._report_repository_line_endings(report, reporter.pass_, reporter.warn_, reporter.fail_)
    assert len(reporter.findings) == 2
    assert reporter.findings[0].check_id == "git.attributes-policy-unknown"
    assert reporter.findings[1].severity == (
        "fail" if other in ("stale-index", "crlf-mismatch") else "warn"
    )


@pytest.mark.parametrize("waiver", ["unknown", "old", "expired", "wrong-subject"])
def test_unknown_waiver_is_separate_from_real_conflicts(tmp_path, waiver):
    root, data, _local = _checkout(tmp_path)
    _missing_sibling(root, tmp_path)
    (root / ".gitattributes").write_text("*.txt -text\n")
    check = "git.line-endings-unreadable" if waiver == "old" else "git.attributes-policy-unknown"
    subject = "project-data" if waiver == "wrong-subject" else "project-checkout"
    expiry = "expires = 2000-01-01" if waiver == "expired" else "permanent = true"
    (data / "doctor-waivers.toml").write_text(
        f'version = 1\n[[waiver]]\ncheck = "{check}"\nsubject = "{subject}"\nreason = "Accepted limitation"\n{expiry}\n'
    )
    reporter = _reporter(data)
    doctor._check_line_endings(
        root, reporter.pass_, reporter.warn_, reporter.skip_, reporter.fail_, booley_dir=data
    )
    findings = {f.check_id: f for f in reporter.findings}
    assert findings["git.attributes-policy-unknown"].severity == (
        "waived" if waiver == "unknown" else "warn"
    )
    assert findings["git.line-endings-unreadable"].severity == (
        "waived" if waiver == "old" else "warn"
    )
    assert not reporter.result(0).clean


@pytest.mark.parametrize("check_only", [False, True])
@pytest.mark.parametrize("layout", ["single", "separate", "pin"])
def test_init_unknown_summary_is_nonfatal_and_accurate(tmp_path, capsys, check_only, layout):
    root, data, _local = _checkout(tmp_path)
    _missing_sibling(root, tmp_path)
    if layout == "pin":
        _git(root, "config", "--unset", "core.autocrlf")
    if layout == "separate":
        separate = tmp_path / "booley-data"
        separate.mkdir()
        (separate / "booley.toml").write_text((data / "booley.toml").read_text())
        _git(separate, "init", "-q")
        _git(separate, "config", "core.autocrlf", "false")
        (separate / ".gitattributes").write_text("* text=auto eol=lf\n")
        data = separate
    ctx = InitContext(project_root=root, check_only=check_only, interactive=False)
    git_hooks._step_line_endings(ctx, data)
    result = ctx.results[-1]
    assert result.status == "warn" and "inspection incomplete" in result.detail
    if layout == "separate":
        assert "project-checkout" in result.detail
    assert init_cmd._print_summary(ctx) == int(check_only)
    output = capsys.readouterr().out
    assert "working tree is container-safe" not in output.split("Booley Directory")[0]
    if check_only:
        verdict = output.strip().splitlines()[-1]
        assert verdict.startswith("Checked initialization: pending or unresolved — ")
        assert f"{result.name}: {result.detail}" in verdict
