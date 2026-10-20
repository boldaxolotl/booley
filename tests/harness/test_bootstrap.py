from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from booley.config.host_config import HostConfigError, SandboxHostPolicy
from booley.harness import bootstrap, bootstrap_cli, init_cmd
from booley.runtime import issuance_invalidation
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildRequest,
    DockerCapacityError,
)
from booley.runtime.image_lifecycle import ImageCleanup, Intent, LifecycleResult, Status
from tests.conftest import require_symlinks


def _current(resource: str) -> bootstrap.BootstrapFinding:
    return bootstrap.BootstrapFinding(resource, bootstrap.BootstrapState.CURRENT, "current")


def _current_sidecars() -> bootstrap.host_sidecars.SidecarResult:
    findings = tuple(
        bootstrap.host_sidecars.SidecarFinding(
            resource,
            bootstrap.host_sidecars.SidecarState.CURRENT,
            "current",
        )
        for resource in ("proxy-image", "reaper-image", "network", "proxy", "reaper")
    )
    return bootstrap.host_sidecars.SidecarResult(findings)


def _prepare_empty_invalidation_store(config_root: Path, project: Path) -> Path:
    project.mkdir()
    pending = issuance_invalidation.prepare(
        str(project.resolve()),
        cleanup_resources=False,
    )
    issuance_invalidation.cancel(pending)
    return config_root / "booley"


def _wire_current(
    monkeypatch: pytest.MonkeyPatch, *, stub_vscode_extension: bool = True
) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "host_capacity_requests", lambda _intent: ())
    monkeypatch.setattr(bootstrap.host_sidecars, "plan_image_builds", lambda _intent: ())
    monkeypatch.setattr(bootstrap, "load_host_policy", lambda **_kwargs: SandboxHostPolicy())
    monkeypatch.setattr(bootstrap, "host_install_error", lambda _source: None)
    monkeypatch.setattr(
        bootstrap,
        "_prerequisite_findings",
        lambda: (_current("git"), _current("docker"), _current("vscode")),
    )
    if stub_vscode_extension:
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_vscode_dev_containers",
            lambda _intent: (
                calls.append("vscode-dev-containers") or _current("vscode-dev-containers")
            ),
        )
    monkeypatch.setattr(
        bootstrap,
        "_reconcile_skills",
        lambda _intent: calls.append("skills") or _current("skills"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_reconcile_nangate",
        lambda _intent: calls.append("nangate45") or _current("nangate45"),
    )
    base = LifecycleResult("booley-sandbox", "sha256:base", Status.CURRENT)
    monkeypatch.setattr(
        bootstrap,
        "_reconcile_base_image",
        lambda _intent, **_kwargs: (
            calls.append("base-image") or base,
            _current("base-image"),
        ),
    )
    monkeypatch.setattr(
        bootstrap.host_sidecars,
        "reconcile_sidecars",
        lambda _policy, _intent, **_kwargs: calls.append("sidecars") or _current_sidecars(),
    )
    return calls


def test_bootstrap_reconciles_resources_in_fixed_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_current(monkeypatch)
    result = bootstrap.reconcile_bootstrap(Intent.CHECK)
    assert calls == [
        "vscode-dev-containers",
        "skills",
        "nangate45",
        "base-image",
        "sidecars",
    ]
    assert [finding.resource for finding in result.findings] == [
        "host-config",
        "git",
        "docker",
        "vscode",
        "vscode-dev-containers",
        "skills",
        "nangate45",
        "base-image",
        "proxy-image",
        "reaper-image",
        "network",
        "proxy",
        "reaper",
    ]
    assert result.ready
    assert result.exit_status == 0


def test_bootstrap_refuses_aggregate_image_plan_before_first_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _wire_current(monkeypatch)
    base = DockerBuildRequest("runtime base", "base")
    sandbox = DockerBuildRequest("Sandbox Image", "sandbox")
    proxy = DockerBuildRequest("proxy", "proxy", BuildEstimateClass.THIN_OVERLAY)
    reaper = DockerBuildRequest("reaper", "reaper", BuildEstimateClass.THIN_OVERLAY)
    monkeypatch.setattr(bootstrap, "host_capacity_requests", lambda _intent: (base, sandbox))
    monkeypatch.setattr(
        bootstrap.host_sidecars,
        "plan_image_builds",
        lambda _intent: (proxy, reaper),
    )
    observed = []

    def refuse(*_args, **kwargs):
        observed.extend(kwargs["remaining_plan"].requests)
        raise DockerCapacityError("capacity refused")

    monkeypatch.setattr(bootstrap, "ensure_docker_build_capacity", refuse)

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert [request.managed_image for request in observed] == [
        "runtime base",
        "Sandbox Image",
        "proxy",
        "reaper",
    ]
    assert "base-image" not in calls
    assert "sidecars" not in calls
    assert result.findings[-1].resource == "image-capacity"


def test_bootstrap_threads_aggregate_plan_through_every_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_current(monkeypatch)
    base = DockerBuildRequest("runtime base", "base")
    proxy = DockerBuildRequest("proxy", "proxy", BuildEstimateClass.THIN_OVERLAY)
    monkeypatch.setattr(bootstrap, "host_capacity_requests", lambda _intent: (base,))
    monkeypatch.setattr(
        bootstrap.host_sidecars,
        "plan_image_builds",
        lambda _intent: (proxy,),
    )
    monkeypatch.setattr(bootstrap, "ensure_docker_build_capacity", lambda *_a, **_kw: None)
    observed = []

    def reconcile_base(_intent, **kwargs):
        observed.append(kwargs["capacity_plan"])
        result = LifecycleResult("booley-sandbox", "sha256:base", Status.CURRENT)
        return result, _current("base-image")

    def reconcile_sidecars(_policy, _intent, **kwargs):
        observed.append(kwargs["capacity_plan"])
        return _current_sidecars()

    monkeypatch.setattr(bootstrap, "_reconcile_base_image", reconcile_base)
    monkeypatch.setattr(bootstrap.host_sidecars, "reconcile_sidecars", reconcile_sidecars)

    assert bootstrap.reconcile_bootstrap(Intent.ENSURE).ready
    assert [plan.requests for plan in observed] == [(base, proxy), (base, proxy)]


def test_noncanonical_install_stops_before_host_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "load_host_policy", lambda **_kwargs: SandboxHostPolicy())
    monkeypatch.setattr(
        bootstrap,
        "host_install_error",
        lambda _source: "temporary QA wheel cannot manage the host",
    )
    monkeypatch.setattr(
        bootstrap,
        "_prerequisite_findings",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe or mutate")),
    )

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert result.exit_status == 2
    assert [finding.resource for finding in result.findings] == ["host-config", "host-install"]


def test_invalid_config_stops_before_any_other_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def invalid(**_kwargs: object):
        raise HostConfigError(tmp_path / "config.toml", "sandbox.max_sessions", "bad")

    monkeypatch.setattr(bootstrap, "load_host_policy", invalid)
    monkeypatch.setattr(
        bootstrap,
        "_prerequisite_findings",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)
    assert result.exit_status == 2
    assert [finding.resource for finding in result.findings] == ["host-config"]


def test_check_only_pending_is_exit_one_but_mutating_pending_is_failure() -> None:
    pending = bootstrap.BootstrapFinding("resource", bootstrap.BootstrapState.PENDING, "work")
    assert bootstrap.BootstrapResult(Intent.CHECK, (pending,)).exit_status == 1
    assert bootstrap.BootstrapResult(Intent.ENSURE, (pending,)).exit_status == 2


def test_warning_is_ready_and_non_blocking() -> None:
    warning = bootstrap.BootstrapFinding(
        "qa-skills", bootstrap.BootstrapState.WARNING, "restore source"
    )
    result = bootstrap.BootstrapResult(Intent.CHECK, (warning,))

    assert result.ready is True
    assert result.exit_status == 0


@pytest.mark.parametrize("flag", ["with_qa_skills", "without_qa_skills"])
def test_check_only_rejects_qa_selection_flags(flag: str, capsys) -> None:
    args = SimpleNamespace(check_only=True, update=False, **{flag: True})

    assert bootstrap_cli.run_bootstrap(args) == 2
    assert "cannot be combined" in capsys.readouterr().out


def test_opt_out_prunes_qa_before_failing_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "load_host_policy", lambda **_kwargs: SandboxHostPolicy())
    monkeypatch.setattr(bootstrap, "host_install_error", lambda _source: None)
    monkeypatch.setattr(
        bootstrap,
        "_reconcile_qa_skills",
        lambda *_args, **_kwargs: calls.append("qa-prune") or _current("qa-skills"),
    )
    prerequisite = bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState.ERROR, "offline")
    monkeypatch.setattr(
        bootstrap,
        "_prerequisite_findings",
        lambda: (calls.append("prerequisites") or prerequisite,),
    )

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE, qa_opt_out=True)

    assert calls == ["qa-prune", "prerequisites"]
    assert [finding.resource for finding in result.findings] == [
        "host-config",
        "qa-skills",
        "docker",
    ]


_QA_WARNING = bootstrap.BootstrapFinding("qa-skills", bootstrap.BootstrapState.WARNING, "conflict")
_QA_PRUNED = bootstrap.BootstrapFinding("qa-skills", bootstrap.BootstrapState.CHANGED, "pruned")
_DOCKER_ERROR = bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState.ERROR, "offline")


@pytest.mark.parametrize(
    ("with_qa", "findings", "selection_error", "status", "events"),
    [
        (True, (), None, 0, ["register", "enable", "reconcile"]),
        (True, (_QA_WARNING,), None, 2, ["register", "enable", "reconcile"]),
        (False, (_QA_WARNING,), None, 2, ["register", "reconcile"]),
        (True, (), "invalid", 2, ["register", "enable"]),
        (False, (_DOCKER_ERROR,), None, 2, ["register", "reconcile"]),
        (False, (_QA_PRUNED, _DOCKER_ERROR), None, 2, ["register", "reconcile", "disable"]),
        (False, (_QA_PRUNED,), "read-only", 2, ["register", "reconcile", "disable"]),
    ],
    ids=[
        "enable-persists-before-reconciliation",
        "enable-warning-overrides-nonblocking-status",
        "opt-out-warning-overrides-nonblocking-status",
        "enable-failure-skips-reconciliation",
        "opt-out-keeps-selection-when-prune-did-not-run",
        "opt-out-removes-selection-after-prune-despite-later-error",
        "opt-out-reports-selection-removal-failure",
    ],
)
def test_qa_selection_flags_order_and_status(
    monkeypatch, capsys, with_qa, findings, selection_error, status, events
) -> None:
    observed: list[str] = []

    def change_selection(event: str, error_type: type[Exception]) -> None:
        observed.append(event)
        if selection_error:
            raise error_type(selection_error)

    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda *_a, **_kw: observed.append("register"),
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "enable_qa_skills",
        lambda _cwd: change_selection("enable", bootstrap_cli.QaSkillSelectionError),
    )
    monkeypatch.setattr(
        bootstrap_cli, "disable_qa_skills", lambda: change_selection("disable", OSError)
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda intent, **_kw: (
            observed.append("reconcile") or bootstrap.BootstrapResult(intent, findings)
        ),
    )
    args = SimpleNamespace(with_qa_skills=with_qa, without_qa_skills=not with_qa)

    assert bootstrap_cli.run_bootstrap(args) == status
    assert observed == events
    if selection_error:
        assert f"Cannot {events[-1]} QA skills: {selection_error}" in capsys.readouterr().out


def test_qa_reconciliation_relinks_on_refresh_and_prunes_on_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[Path, frozenset[str], bool]] = []
    monkeypatch.setattr(bootstrap, "skills_dir", lambda: tmp_path / "packaged")
    recorded = Mock(return_value=Path("/qa"))
    monkeypatch.setattr(bootstrap, "active_qa_source_root", recorded)

    def reconcile(source, *, names, allow_retarget, **_kwargs):
        observed.append((source, names, allow_retarget))
        return ()

    monkeypatch.setattr(bootstrap, "reconcile_host_qa_skills", reconcile)

    calls = ((Intent.ENSURE, False), (Intent.REFRESH, False), (Intent.ENSURE, True))
    for intent, opt_out in calls:
        finding = bootstrap._reconcile_qa_skills(intent, opt_out=opt_out)
        assert finding is not None and finding.state is bootstrap.BootstrapState.CURRENT
    assert observed == [
        (Path("/qa"), bootstrap.QA_SKILL_NAMES, False),
        (Path("/qa"), bootstrap.QA_SKILL_NAMES, True),
        (tmp_path / "packaged", frozenset(), False),
    ]
    assert recorded.call_count == 2  # opt-out never reads a possibly corrupt selection


def _record_qa_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record a complete source checkout in the current release's selection format."""
    _wire_current(monkeypatch)
    monkeypatch.setattr(bootstrap_cli, "register_host_installation", Mock())
    root = tmp_path / "Booley"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    for name in bootstrap.QA_SKILL_NAMES:
        (root / "qa" / name).mkdir(parents=True)
        (root / "qa" / name / "SKILL.md").write_text("# QA\n")
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    for agent in (".agents", ".claude"):
        (home / agent / "skills").mkdir(parents=True)
    sample = Path(__file__).parents[1] / "runtime" / "fixtures" / "qa-selection-v1.json"
    document = {**json.loads(sample.read_text()), "checkout_root": str(root)}
    (home / ".agents" / "booley-qa-skills.json").write_text(json.dumps(document))
    return root


def test_enable_links_every_agent_folder_and_refuses_linked_worktree(
    tmp_path, monkeypatch, capsys
) -> None:
    root = _record_qa_checkout(tmp_path, monkeypatch)
    selection = tmp_path / "home" / ".agents" / "booley-qa-skills.json"
    (root / ".git").mkdir()  # primary checkout; branch, cleanliness and HEAD are not inspected
    monkeypatch.chdir(root / "qa")
    enable = SimpleNamespace(with_qa_skills=True)

    assert bootstrap_cli.run_bootstrap(enable) == 0
    recorded = selection.read_bytes()
    assert json.loads(recorded) == {"checkout_root": str(root)}
    for agent in (".agents", ".claude"):
        for name in bootstrap.QA_SKILL_NAMES:
            assert (tmp_path / "home" / agent / "skills" / name).resolve() == root / "qa" / name
    (root / ".git").rmdir()
    (root / ".git").write_text("gitdir: elsewhere\n")  # linked worktree links would dangle
    assert bootstrap_cli.run_bootstrap(enable) == 2
    assert "primary Booley source checkout" in capsys.readouterr().out
    assert selection.read_bytes() == recorded


@pytest.mark.parametrize("equivalent", [False, True])
def test_force_does_not_adopt_unrecorded_qa_links(tmp_path, monkeypatch, capsys, equivalent):
    require_symlinks(tmp_path)
    skill = _record_qa_checkout(tmp_path, monkeypatch) / "qa" / "booley-qa-run"
    if equivalent:
        skill = shutil.copytree(skill, tmp_path / "copy" / "booley-qa-run")
    link = tmp_path / "home" / ".agents" / "skills" / "booley-qa-run"
    link.symlink_to(skill, target_is_directory=True)

    assert bootstrap_cli.run_bootstrap(SimpleNamespace(force=True)) == 0
    assert "desired skill name is occupied" in capsys.readouterr().out
    assert link.resolve() == skill.resolve()  # Windows readlink adds a \\?\ prefix
    manifest = json.loads((link.parent / ".booley-qa-skill-links.json").read_text())
    assert "booley-qa-run" not in manifest["links"]


# "." moves the whole recorded checkout away; pyproject.toml carries its source marker.
@pytest.mark.parametrize(
    "missing", [".", "pyproject.toml", *(f"qa/{n}" for n in sorted(bootstrap.QA_SKILL_NAMES))]
)
@pytest.mark.parametrize("consumer", ["plain", "check-only", "init"])
def test_missing_qa_source_warns_without_git(tmp_path, monkeypatch, consumer, missing) -> None:
    root = _record_qa_checkout(tmp_path, monkeypatch)
    for probe in ("_inspect_installation", "_inspect_skills", "_inspect_nangate"):
        monkeypatch.setattr(bootstrap, probe, lambda: _current("other"))
    bootstrap.reconcile_bootstrap(Intent.ENSURE)  # record owned QA links first
    skills = tmp_path / "home" / ".agents" / "skills"

    def entries() -> dict[Path, object]:  # realpath covers junctions; lower: Windows case drift
        return {
            f: f.read_bytes() if f.suffix == ".json" else os.path.realpath(f).lower()
            for f in skills.iterdir()
        }

    owned = entries()
    (root / missing).rename(tmp_path / "moved")
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("QA ran Git")))
    result = {
        "plain": lambda: bootstrap.reconcile_bootstrap(Intent.ENSURE),
        "check-only": lambda: bootstrap.inspect_bootstrap(include_images=False),
        "init": lambda: init_cmd._reconcile_init_bootstrap(
            init_cmd.InitContext(project_root=tmp_path), SimpleNamespace()
        ),
    }[consumer]()

    assert len(owned) > len(bootstrap.QA_SKILL_NAMES)  # QA links plus their manifest
    assert result is not None and result.exit_status == 0
    qa = [finding for finding in result.findings if finding.resource == "qa-skills"]
    assert [finding.state for finding in qa] == [bootstrap.BootstrapState.WARNING]
    assert "--without-qa-skills" in qa[0].detail
    assert entries() == owned  # a missing skill never prunes owned links


def test_public_adapter_uses_refresh_for_force(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Intent] = []
    monkeypatch.setattr(bootstrap_cli, "register_host_installation", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: seen.append(intent) or bootstrap.BootstrapResult(intent, ()),
    )
    assert (
        bootstrap_cli.run_bootstrap(SimpleNamespace(force=True, check_only=False, verbose=False))
        == 0
    )
    assert seen == [Intent.REFRESH]


def test_public_adapter_registers_before_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    identity = SimpleNamespace(version="1.2.3", payload_fingerprint="f" * 64)
    monkeypatch.setattr(bootstrap_cli, "skills_dir", lambda: Path("/installed/skills"))
    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda _source, *, update: events.append(f"register:{update}") or identity,
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: (
            events.append("reconcile") or bootstrap.BootstrapResult(intent, ())
        ),
    )

    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(
            force=False,
            check_only=False,
            verbose=False,
        )
    )

    assert status == 0
    assert events == ["register:False", "reconcile"]


def test_public_adapter_recovers_before_registering_installation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime import session_refresh

    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda *, read_only: not read_only,
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda *_a, **_kw: pytest.fail("recovery must block installation registration"),
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda *_a, **_kw: pytest.fail("recovery must block reconciliation"),
    )

    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(force=False, check_only=False, verbose=False)
    )

    assert status == 2
    assert "Recovered interrupted Sandbox host state" in capsys.readouterr().out


def test_public_adapter_explicitly_updates_registered_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    identity = SimpleNamespace(version="2.0.0", payload_fingerprint="a" * 64)
    monkeypatch.setattr(bootstrap_cli, "skills_dir", lambda: Path("/installed/skills"))
    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda _source, *, update: events.append(f"register:{update}") or identity,
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: (
            events.append("reconcile") or bootstrap.BootstrapResult(intent, ())
        ),
    )

    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(
            force=False,
            check_only=False,
            verbose=False,
            update=True,
        )
    )

    assert status == 0
    assert events == ["register:True", "reconcile"]


def test_public_adapter_rejects_update_in_check_only(capsys):
    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(
            force=False,
            check_only=True,
            verbose=False,
            update=True,
        )
    )

    assert status == 2
    assert "check-only" in capsys.readouterr().out


def test_public_adapter_check_only_does_not_register(monkeypatch):
    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda *_a, **_kw: pytest.fail("check-only must not register an installation"),
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "inspect_bootstrap",
        lambda **_kwargs: bootstrap.BootstrapResult(Intent.CHECK, ()),
    )

    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(force=False, check_only=True, verbose=False)
    )

    assert status == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_public_adapter_check_only_reports_unsafe_private_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    booley_config = _prepare_empty_invalidation_store(config_root, tmp_path / "project")
    booley_config.chmod(0o755)
    expected = bootstrap.BootstrapResult(Intent.CHECK, (_current("ready"),))
    monkeypatch.setattr(bootstrap_cli, "inspect_bootstrap", lambda *_a, **_kw: expected)

    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(force=False, check_only=True, verbose=False)
    )

    captured = capsys.readouterr()
    assert status == 2
    assert "[XX]" in captured.out
    assert str(booley_config) in captured.out
    assert "0755" in captured.out
    assert f"chmod 700 {booley_config}" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_public_adapter_check_only_accepts_repaired_private_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    booley_config = _prepare_empty_invalidation_store(config_root, tmp_path / "project")
    booley_config.chmod(0o700)
    expected = bootstrap.BootstrapResult(Intent.CHECK, (_current("ready"),))
    monkeypatch.setattr(bootstrap_cli, "inspect_bootstrap", lambda *_a, **_kw: expected)

    assert (
        bootstrap_cli.run_bootstrap(SimpleNamespace(force=False, check_only=True, verbose=False))
        == 0
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_public_adapter_check_only_accepts_shared_xdg_root_at_0755(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    _prepare_empty_invalidation_store(config_root, tmp_path / "project")
    config_root.chmod(0o755)
    expected = bootstrap.BootstrapResult(Intent.CHECK, (_current("ready"),))
    monkeypatch.setattr(bootstrap_cli, "inspect_bootstrap", lambda *_a, **_kw: expected)

    assert (
        bootstrap_cli.run_bootstrap(SimpleNamespace(force=False, check_only=True, verbose=False))
        == 0
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_public_adapter_mutation_reports_unsafe_private_store_before_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    booley_config = _prepare_empty_invalidation_store(config_root, tmp_path / "project")
    booley_config.chmod(0o755)
    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda *_a, **_kw: pytest.fail("unsafe recovery must precede registration"),
    )

    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(force=False, check_only=False, verbose=False)
    )

    captured = capsys.readouterr()
    assert status == 2
    assert "[XX]" in captured.out
    assert str(booley_config) in captured.out
    assert "0755" in captured.out
    assert f"chmod 700 {booley_config}" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_public_adapter_reports_registration_failure(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap_cli, "skills_dir", lambda: Path("/installed/skills"))
    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda _source, *, update: (_ for _ in ()).throw(
            bootstrap_cli.HostInstallationError("registration failed")
        ),
    )

    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(
            force=False,
            check_only=False,
            verbose=False,
        )
    )

    assert status == 2
    assert "registration failed" in capsys.readouterr().out


def test_skill_reconciliation_rejects_noncanonical_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "skills"
    source.mkdir()
    monkeypatch.setattr(bootstrap, "skills_dir", lambda: source)
    monkeypatch.setattr(bootstrap, "host_install_error", lambda _source: "not canonical")

    finding = bootstrap._reconcile_skills(Intent.CHECK)

    assert finding.state is bootstrap.BootstrapState.ERROR
    assert finding.detail == "not canonical"


def test_vscode_requires_an_executable_or_installed_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from booley.config import editor

    (tmp_path / ".config" / "Code").mkdir(parents=True)
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: None)
    monkeypatch.setattr(editor, "resolve_editor_install", lambda: None)

    finding = bootstrap._vscode_finding()

    assert finding.state is bootstrap.BootstrapState.ERROR


def test_vscode_accepts_a_proven_gui_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from booley.config import editor

    application = tmp_path / "Visual Studio Code.app"
    executable = application / "Contents" / "MacOS" / "Electron"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: None)
    monkeypatch.setattr(editor, "resolve_editor_install", lambda: application)

    finding = bootstrap._vscode_finding()

    assert finding.state is bootstrap.BootstrapState.CURRENT
    assert application.name in finding.detail


@pytest.mark.parametrize(
    "failed_resource",
    ["git", "vscode-dev-containers", "skills", "nangate45", "base-image"],
)
def test_bootstrap_stops_after_each_failed_dependency(
    monkeypatch: pytest.MonkeyPatch,
    failed_resource: str,
) -> None:
    calls = _wire_current(monkeypatch)
    error = bootstrap.BootstrapFinding(
        failed_resource,
        bootstrap.BootstrapState.ERROR,
        "failed",
    )
    if failed_resource == "git":
        monkeypatch.setattr(
            bootstrap,
            "_prerequisite_findings",
            lambda: (error, _current("docker"), _current("vscode")),
        )
        expected_calls: list[str] = []
    elif failed_resource == "vscode-dev-containers":
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_vscode_dev_containers",
            lambda _intent: calls.append("vscode-dev-containers") or error,
        )
        expected_calls = ["vscode-dev-containers"]
    elif failed_resource == "skills":
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_skills",
            lambda _intent: calls.append("skills") or error,
        )
        expected_calls = ["vscode-dev-containers", "skills"]
    elif failed_resource == "nangate45":
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_nangate",
            lambda _intent: calls.append("nangate45") or error,
        )
        expected_calls = ["vscode-dev-containers", "skills", "nangate45"]
    else:
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_base_image",
            lambda _intent, **_kwargs: (calls.append("base-image") or None, error),
        )
        expected_calls = [
            "vscode-dev-containers",
            "skills",
            "nangate45",
            "base-image",
        ]

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert result.exit_status == 2
    assert calls == expected_calls
    assert error in result.findings


def test_prerequisites_replace_a_valid_docker_probe_with_daemon_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "_git_finding", lambda: _current("git"))
    monkeypatch.setattr(bootstrap, "_tool_finding", lambda name, _arg: _current(name))
    monkeypatch.setattr(bootstrap, "_docker_daemon_error", lambda: "daemon unavailable")
    monkeypatch.setattr(bootstrap, "_vscode_finding", lambda: _current("vscode"))

    findings = bootstrap._prerequisite_findings()

    assert findings[1] == bootstrap.BootstrapFinding(
        "docker", bootstrap.BootstrapState.ERROR, "daemon unavailable"
    )


@pytest.mark.parametrize(
    "version_line",
    [
        "git version 2.37.2",
        "git version 2.37.2.windows.1",
        "git version 2.46.0 (Apple Git-152)",
    ],
)
def test_git_probe_accepts_supported_stable_versions(
    monkeypatch: pytest.MonkeyPatch,
    version_line: str,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, version_line, ""),
    )

    finding = bootstrap._git_finding()

    assert finding.state is bootstrap.BootstrapState.CURRENT
    assert finding.detail == version_line


@pytest.mark.parametrize(
    "version_line",
    [
        "git version 2.37.0.windows.1",
        "git version 2.36.9",
    ],
)
def test_git_probe_rejects_older_versions(
    monkeypatch: pytest.MonkeyPatch,
    version_line: str,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, version_line, ""),
    )

    finding = bootstrap._git_finding()

    assert finding.state is bootstrap.BootstrapState.ERROR
    assert "Git 2.37.2 or newer" in finding.detail
    assert "Upgrade Git" in finding.detail


@pytest.mark.parametrize(
    "version_line",
    [
        "git version 2.37.2.rc0",
        "git version 2.40.0-beta1",
        "git version unknown",
        "not git version 2.46.0",
    ],
)
def test_git_probe_rejects_prerelease_and_unparseable_versions(
    monkeypatch: pytest.MonkeyPatch,
    version_line: str,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, version_line, ""),
    )

    finding = bootstrap._git_finding()

    assert finding.state is bootstrap.BootstrapState.ERROR
    assert "Git 2.37.2 or newer" in finding.detail


@pytest.mark.parametrize(
    ("version_line", "expected"),
    [
        ("git version 2.47.9", (2, 47, 9)),
        ("git version 2.48.0", (2, 48, 0)),
        ("git version 2.53.0.windows.1", (2, 53, 0)),
        ("git version 2.50.1 (Apple Git-155)", (2, 50, 1)),
        ("git version 2.48.0-rc1", None),
        ("git version 2.48.0.beta2", None),
        ("git version unknown", None),
    ],
)
def test_parse_git_version_is_strict_capability_evidence(
    version_line: str,
    expected: tuple[int, int, int] | None,
) -> None:
    assert bootstrap.parse_git_version(version_line) == expected


def test_git_probe_reports_missing_and_execution_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    assert "Git 2.37.2 or newer" in bootstrap._git_finding().detail

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "bad"),
    )
    assert "probe failed" in bootstrap._git_finding().detail

    def fail(*_args, **_kwargs):
        raise OSError("denied")

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    assert "cannot run Git" in bootstrap._git_finding().detail


def test_tool_probe_reports_missing_failure_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    assert bootstrap._tool_finding("git", "--version").state is bootstrap.BootstrapState.ERROR

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "bad"),
    )
    assert "probe failed" in bootstrap._tool_finding("git", "--version").detail

    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", "tool 1.2\nmore"),
    )
    assert bootstrap._tool_finding("git", "--version").detail == "tool 1.2"


def test_tool_probe_wraps_execution_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/tool")

    def fail(*_args, **_kwargs):
        raise OSError("denied")

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    assert "cannot run git" in bootstrap._tool_finding("git", "--version").detail


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (subprocess.TimeoutExpired("docker", 10), "did not respond"),
        (OSError("denied"), "cannot contact"),
    ],
)
def test_docker_daemon_wraps_probe_errors(
    monkeypatch: pytest.MonkeyPatch,
    outcome: BaseException,
    expected: str,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")

    def fail(*_args, **_kwargs):
        raise outcome

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    assert expected in (bootstrap._docker_daemon_error() or "")


def test_docker_daemon_reports_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    assert bootstrap._docker_daemon_error() is None

    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "daemon stopped\nmore"),
    )
    assert bootstrap._docker_daemon_error() == (
        "Docker daemon is not running or accessible: daemon stopped"
    )


def test_vscode_accepts_a_path_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from booley.config import editor

    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/codium")

    finding = bootstrap._vscode_finding()

    assert finding.state is bootstrap.BootstrapState.CURRENT
    assert finding.detail == "codium available"


def test_public_bootstrap_rejects_an_editor_without_a_management_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.config import editor

    calls = _wire_current(monkeypatch, stub_vscode_extension=False)
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: None)

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert result.findings[-1].resource == "vscode-dev-containers"
    assert result.findings[-1].state is bootstrap.BootstrapState.ERROR
    assert "management command" in result.findings[-1].detail
    assert calls == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (subprocess.TimeoutExpired("code", 30), "within 30 seconds"),
        (OSError("denied"), "cannot inspect"),
    ],
)
def test_public_bootstrap_reports_extension_probe_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: str,
) -> None:
    from booley.config import editor

    _wire_current(monkeypatch, stub_vscode_extension=False)
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert result.findings[-1].state is bootstrap.BootstrapState.ERROR
    assert expected in result.findings[-1].detail


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (subprocess.TimeoutExpired("code", 120), "within 120 seconds"),
        (OSError("denied"), "cannot install"),
    ],
)
def test_public_bootstrap_reports_extension_install_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: str,
) -> None:
    from booley.config import editor

    _wire_current(monkeypatch, stub_vscode_extension=False)
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")
    calls = 0

    def fail_after_probe(argv, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(argv, 0, "ms-python.python\n", "")
        raise failure

    monkeypatch.setattr(bootstrap.subprocess, "run", fail_after_probe)

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert result.findings[-1].state is bootstrap.BootstrapState.ERROR
    assert expected in result.findings[-1].detail


@pytest.mark.parametrize("verification_error", [False, True])
def test_public_bootstrap_rejects_an_unverified_install(
    monkeypatch: pytest.MonkeyPatch,
    verification_error: bool,
) -> None:
    from booley.config import editor

    _wire_current(monkeypatch, stub_vscode_extension=False)
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")
    calls = 0

    def run(argv, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if calls == 3 and verification_error:
            raise OSError("verification denied")
        return subprocess.CompletedProcess(argv, 0, "ms-python.python\n", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert result.findings[-1].state is bootstrap.BootstrapState.ERROR
    if verification_error:
        assert "verification denied" in result.findings[-1].detail
    else:
        assert "did not report the extension" in result.findings[-1].detail


def test_vscode_dev_containers_accepts_an_existing_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.config import editor

    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "MS-VSCODE-REMOTE.REMOTE-CONTAINERS\n", ""
        ),
    )

    finding = bootstrap._reconcile_vscode_dev_containers(Intent.CHECK)

    assert finding.state is bootstrap.BootstrapState.CURRENT
    assert bootstrap.DEV_CONTAINERS_EXTENSION_ID in finding.detail


def test_vscode_dev_containers_check_reports_pending_without_installing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.config import editor

    calls: list[list[str]] = []
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ms-python.python\n", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)

    finding = bootstrap._reconcile_vscode_dev_containers(Intent.CHECK)

    assert finding.state is bootstrap.BootstrapState.PENDING
    assert calls == [["/usr/bin/code", "--list-extensions"]]


def test_vscode_dev_containers_is_installed_and_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.config import editor

    calls: list[list[str]] = []
    outputs = iter(("ms-python.python\n", "", f"{bootstrap.DEV_CONTAINERS_EXTENSION_ID}\n"))
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, next(outputs), "")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)

    finding = bootstrap._reconcile_vscode_dev_containers(Intent.ENSURE)

    assert finding.state is bootstrap.BootstrapState.CHANGED
    assert calls == [
        ["/usr/bin/code", "--list-extensions"],
        [
            "/usr/bin/code",
            "--install-extension",
            bootstrap.DEV_CONTAINERS_EXTENSION_ID,
            "--force",
        ],
        ["/usr/bin/code", "--list-extensions"],
    ]


def test_vscode_dev_containers_reports_cli_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.config import editor

    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "marketplace offline\n"),
    )

    finding = bootstrap._reconcile_vscode_dev_containers(Intent.ENSURE)

    assert finding.state is bootstrap.BootstrapState.ERROR
    assert "marketplace offline" in finding.detail


def test_vscode_dev_containers_reports_install_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.config import editor

    results = iter(
        (
            subprocess.CompletedProcess([], 0, "ms-python.python\n", ""),
            subprocess.CompletedProcess([], 1, "", "marketplace offline\n"),
        )
    )
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/usr/bin/code")
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *_args, **_kwargs: next(results))

    finding = bootstrap._reconcile_vscode_dev_containers(Intent.ENSURE)

    assert finding.state is bootstrap.BootstrapState.ERROR
    assert finding.detail == "Dev Containers extension installation failed: marketplace offline"


def test_skill_reconciliation_reports_missing_pending_changed_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(bootstrap, "skills_dir", lambda: missing)
    assert bootstrap._reconcile_skills(Intent.CHECK).state is bootstrap.BootstrapState.ERROR

    source = tmp_path / "skills"
    source.mkdir()
    monkeypatch.setattr(bootstrap, "skills_dir", lambda: source)
    monkeypatch.setattr(bootstrap, "host_install_error", lambda _source: None)
    changed_event = SimpleNamespace(changed=True, failed=False, detail="", name="linked")
    report = SimpleNamespace(events=(changed_event,), diagnostics=(), fatal=None)
    target = tmp_path / ".agents" / "skills"
    reconciliation = SimpleNamespace(target=target, report=report)
    monkeypatch.setattr(
        bootstrap, "reconcile_host_skills", lambda *_args, **_kwargs: (reconciliation,)
    )
    assert bootstrap._reconcile_skills(Intent.CHECK).state is bootstrap.BootstrapState.PENDING
    assert bootstrap._reconcile_skills(Intent.ENSURE).state is bootstrap.BootstrapState.CHANGED

    failed_event = SimpleNamespace(changed=False, failed=True, detail="broken", name="link")
    failed_report = SimpleNamespace(
        events=(failed_event,), diagnostics=("diagnostic",), fatal="fatal"
    )
    monkeypatch.setattr(
        bootstrap,
        "reconcile_host_skills",
        lambda *_args, **_kwargs: (SimpleNamespace(target=target, report=failed_report),),
    )
    finding = bootstrap._reconcile_skills(Intent.ENSURE)
    assert finding.state is bootstrap.BootstrapState.ERROR
    assert finding.detail == f"{target}: broken; diagnostic; fatal"


def test_nangate_reconciliation_covers_current_check_fetch_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(bootstrap.nangate_pdk, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.nangate_pdk, "secure_config_dir_for_cache", lambda _root: False)
    monkeypatch.setattr(bootstrap.nangate_pdk, "validation_errors", lambda _root: ())
    assert bootstrap._reconcile_nangate(Intent.CHECK).state is bootstrap.BootstrapState.CURRENT

    monkeypatch.setattr(
        bootstrap.nangate_pdk, "validation_errors", lambda _root: ("missing archive",)
    )
    pending = bootstrap._reconcile_nangate(Intent.CHECK)
    assert pending.state is bootstrap.BootstrapState.PENDING
    assert "missing archive" in pending.detail

    monkeypatch.setattr(bootstrap.nangate_pdk, "fetch", lambda _root: None)
    assert bootstrap._reconcile_nangate(Intent.ENSURE).state is bootstrap.BootstrapState.CHANGED

    def fail(_root):
        raise bootstrap.nangate_pdk.NangatePdkError("download failed")

    monkeypatch.setattr(bootstrap.nangate_pdk, "fetch", fail)
    assert bootstrap._reconcile_nangate(Intent.ENSURE).state is bootstrap.BootstrapState.ERROR


@pytest.mark.skipif(os.name == "nt", reason="Windows mode bits do not describe ACLs")
def test_nangate_reconciliation_hardens_config_for_existing_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config_root = tmp_path / "booley"
    config_root.mkdir(mode=0o755)
    monkeypatch.setattr(bootstrap.nangate_pdk, "validation_errors", lambda _root: ())

    finding = bootstrap._reconcile_nangate(Intent.ENSURE)

    assert finding.state is bootstrap.BootstrapState.CHANGED
    assert stat.S_IMODE(config_root.stat().st_mode) == 0o700


def test_nangate_reconciliation_reports_config_security_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(bootstrap.nangate_pdk, "cache_root", lambda: tmp_path)

    def fail(_root):
        raise bootstrap.nangate_pdk.NangatePdkError("unsafe config root")

    monkeypatch.setattr(bootstrap.nangate_pdk, "secure_config_dir_for_cache", fail)

    finding = bootstrap._reconcile_nangate(Intent.ENSURE)

    assert finding.state is bootstrap.BootstrapState.ERROR
    assert finding.detail == "unsafe config root"


@pytest.mark.parametrize(
    ("image_status", "bootstrap_state"),
    [
        (Status.CURRENT, bootstrap.BootstrapState.CURRENT),
        (Status.STALE, bootstrap.BootstrapState.PENDING),
        (Status.CHANGED, bootstrap.BootstrapState.CHANGED),
        (Status.EXTERNAL, bootstrap.BootstrapState.ERROR),
    ],
)
def test_base_image_status_mapping(
    monkeypatch: pytest.MonkeyPatch,
    image_status: Status,
    bootstrap_state: bootstrap.BootstrapState,
) -> None:
    result = LifecycleResult("base", "sha256:id", image_status)
    monkeypatch.setattr(bootstrap, "reconcile_images", lambda *_args, **_kwargs: result)
    actual, finding = bootstrap._reconcile_base_image(Intent.CHECK, verbose=False)
    assert actual is result
    assert finding.state is bootstrap_state


def test_base_image_failure_becomes_typed_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise bootstrap.ImageLifecycleError("inspect failed")

    monkeypatch.setattr(bootstrap, "reconcile_images", fail)
    result, finding = bootstrap._reconcile_base_image(Intent.CHECK, verbose=False)
    assert result is None
    assert finding == bootstrap.BootstrapFinding(
        "base-image", bootstrap.BootstrapState.ERROR, "inspect failed"
    )


def test_base_image_check_reports_pending_release_tag_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"
    result = LifecycleResult(
        "booley-sandbox",
        "sha256:id",
        Status.CURRENT,
        cleanup=ImageCleanup(pending=(release,)),
    )
    monkeypatch.setattr(bootstrap, "reconcile_images", lambda *_args, **_kwargs: result)

    actual, finding = bootstrap._reconcile_base_image(Intent.CHECK, verbose=False)

    assert actual is result
    assert finding.state is bootstrap.BootstrapState.PENDING
    assert finding.detail == f"obsolete Docker image tag can be removed: {release}"


def test_base_image_reports_removed_and_container_retained_release_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"
    retained = "ghcr.io/boldaxolotl/booley-sandbox:0.2.4"
    result = LifecycleResult(
        "booley-sandbox",
        "sha256:id",
        Status.CHANGED,
        cleanup=ImageCleanup(removed=(removed,), retained_required=(retained,)),
    )
    monkeypatch.setattr(bootstrap, "reconcile_images", lambda *_args, **_kwargs: result)

    actual, finding = bootstrap._reconcile_base_image(Intent.ENSURE, verbose=False)

    assert actual is result
    assert finding.state is bootstrap.BootstrapState.CHANGED
    assert finding.detail == (
        f"removed obsolete Docker image tags: {removed}; "
        f"retained Docker image tags still required by containers: {retained}"
    )


@pytest.mark.parametrize(
    ("sidecar_state", "bootstrap_state"),
    [
        (bootstrap.host_sidecars.SidecarState.CURRENT, bootstrap.BootstrapState.CURRENT),
        (bootstrap.host_sidecars.SidecarState.PENDING, bootstrap.BootstrapState.PENDING),
        (bootstrap.host_sidecars.SidecarState.CHANGED, bootstrap.BootstrapState.CHANGED),
        (bootstrap.host_sidecars.SidecarState.ERROR, bootstrap.BootstrapState.ERROR),
    ],
)
def test_sidecar_state_mapping(sidecar_state, bootstrap_state) -> None:
    finding = bootstrap._sidecar_finding(
        bootstrap.host_sidecars.SidecarFinding("proxy", sidecar_state, "detail")
    )
    assert finding == bootstrap.BootstrapFinding("proxy", bootstrap_state, "detail")


@pytest.mark.parametrize(
    ("state", "expected_message"),
    [
        (bootstrap.BootstrapState.CURRENT, "is current"),
        (bootstrap.BootstrapState.PENDING, "pending work"),
        (bootstrap.BootstrapState.WARNING, "is current"),
        (bootstrap.BootstrapState.ERROR, "is incomplete"),
    ],
)
def test_cli_renders_each_exit_class(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: bootstrap.BootstrapState,
    expected_message: str,
) -> None:
    result = bootstrap.BootstrapResult(
        Intent.CHECK,
        (bootstrap.BootstrapFinding("resource", state, "detail"),),
    )
    monkeypatch.setattr(bootstrap_cli, "inspect_bootstrap", lambda *_args, **_kwargs: result)
    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(force=False, check_only=True, verbose=True)
    )
    output = capsys.readouterr().out
    assert status == result.exit_status
    assert expected_message in output


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("intent", [Intent.CHECK, Intent.ENSURE])
def test_host_policy_migration_warning_reaches_init_and_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    canonical: bool,
    intent: Intent,
) -> None:
    from booley.config.host_config import host_config_path, load_host_policy
    from booley.harness import host_diagnostics, init_cmd

    _wire_current(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = host_config_path()
    path.parent.mkdir()
    text = "[interactive]\nidle_timeout_seconds = 600\nmax_sessions = 2\n"
    if canonical:
        text += "[sandbox]\nmax_sessions = 3\n"
    path.write_text(text)
    monkeypatch.setattr(bootstrap, "load_host_policy", load_host_policy)
    result = bootstrap.reconcile_bootstrap(intent)
    assert result.ready
    assert result.exit_status == 0
    assert result.policy.max_sessions == (3 if canonical else 2)
    warning = next(f for f in result.findings if f.state is bootstrap.BootstrapState.WARNING)
    expected = (
        "[interactive] is deprecated and ignored"
        if canonical
        else "[sandbox]\nidle_timeout_seconds = 600\nmax_sessions = 2"
    )
    assert expected in warning.detail
    ctx = init_cmd.InitContext(project_root=tmp_path, verbose=False)
    init_cmd._record_bootstrap(ctx, result)
    assert expected in capsys.readouterr().out
    report = host_diagnostics.Findings()
    host_diagnostics._add_bootstrap(result, report)
    assert any(
        f.severity.value == "warn" and expected in f.message for f in report.report().findings
    )
    assert path.read_text() == text


@pytest.mark.parametrize("update", [False, True])
def test_cli_bootstrap_registers_persistent_wheel_venv(tmp_path, monkeypatch, update):
    from booley.runtime import host_install, session_refresh
    from booley.runtime.host_install import HostInstallationIdentity

    source = Path(
        "/home/user/.local/share/pipx/venvs/booley-rtl/lib/site-packages/booley/data/skills"
    )
    identity = HostInstallationIdentity(
        1,
        "/home/user/bin/booley",
        "/usr/bin/python3",
        str(source.parents[2]),
        "1.2.3",
        "abc123",
        "f" * 64,
    )
    monkeypatch.setattr(host_install.sys, "prefix", "/home/user/pipx-venv")
    monkeypatch.setattr(host_install.sys, "base_prefix", "/usr")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    monkeypatch.setattr(bootstrap_cli, "skills_dir", lambda: source)
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kwargs: False)
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: bootstrap.BootstrapResult(intent, ()),
    )
    if update:
        from dataclasses import replace

        host_install._write_identity(
            replace(identity, revision="old"), host_install.host_installation_path()
        )
    assert bootstrap_cli.run_bootstrap(SimpleNamespace(update=update)) == 0
    assert host_install.load_host_installation() == identity
    assert host_install.host_install_error(source) is None


@pytest.mark.parametrize("installed", [(), ("claude",), ("codex",), ("claude", "codex")])
def test_host_agent_cli_prerequisite(monkeypatch, tmp_path, installed):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.chdir(tmp_path)
    for name in installed:
        executable = tmp_path / (f"{name}.exe" if os.name == "nt" else name)
        executable.write_text("CLI presence fixture")
        executable.chmod(0o755)
    monkeypatch.setattr(bootstrap, "_git_finding", lambda: _current("git"))
    monkeypatch.setattr(bootstrap, "_tool_finding", lambda name, _arg: _current(name))
    monkeypatch.setattr(bootstrap, "_docker_daemon_error", lambda: None)
    monkeypatch.setattr(bootstrap, "_vscode_finding", lambda: _current("vscode"))
    findings = bootstrap._prerequisite_findings()
    agent = next(finding for finding in findings if finding.resource == "host-agent-cli")
    if installed:
        assert agent.state is bootstrap.BootstrapState.CURRENT
    else:
        assert agent.state is bootstrap.BootstrapState.WARNING
        assert "https://code.claude.com/docs/en/setup" in agent.detail
        assert "https://developers.openai.com/codex/cli" in agent.detail


@pytest.mark.parametrize("intent", [Intent.CHECK, Intent.ENSURE])
def test_missing_host_agent_cli_does_not_block_bootstrap(monkeypatch, intent):
    calls = _wire_current(monkeypatch)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        bootstrap,
        "_prerequisite_findings",
        lambda: (_current("git"), _current("docker"), bootstrap._host_agent_cli_finding()),
    )
    result = bootstrap.reconcile_bootstrap(intent)
    assert result.exit_status == 0
    assert "skills" in calls
    assert "base-image" in calls
    assert any(finding.state is bootstrap.BootstrapState.WARNING for finding in result.findings)


@pytest.mark.parametrize("failure", ["empty-revision", "persisted-validation"])
def test_cli_invalid_registration_never_prints_update_success(
    tmp_path, monkeypatch, capsys, failure
):
    from booley.runtime import host_install, session_refresh

    source = Path(host_install.__file__).resolve().parent.parent / "data/skills"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(
        host_install,
        "current_build_metadata",
        lambda: SimpleNamespace(
            version="1.2.3", revision="previous", payload_fingerprint="f" * 64
        ),
    )
    identity = host_install.current_host_installation(source)
    state = host_install.host_installation_path()
    host_install._write_identity(identity, state)
    before = state.read_bytes()
    monkeypatch.setattr(
        host_install,
        "current_build_metadata",
        lambda: SimpleNamespace(
            version="1.2.3",
            revision="" if failure == "empty-revision" else "new",
            payload_fingerprint="f" * 64,
        ),
    )
    monkeypatch.setattr(bootstrap_cli, "skills_dir", lambda: source)
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kwargs: False)
    calls = []
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda *args, **kwargs: calls.append(args) or bootstrap.BootstrapResult(Intent.ENSURE, ()),
    )
    if failure == "persisted-validation":
        original = host_install.load_host_installation

        def load(path=None):
            if path == state:
                raise host_install.HostInstallationError("persisted validation failed")
            return original(path)

        monkeypatch.setattr(host_install, "load_host_installation", load)
    assert bootstrap_cli.run_bootstrap(SimpleNamespace(update=True)) != 0
    output = capsys.readouterr().out
    assert "Updated canonical Booley host installation" not in output
    assert not calls
    if failure == "empty-revision":
        assert "revision" in output
        assert "complete source checkout" in output
        assert state.read_bytes() == before
        assert host_install.load_host_installation(state) == identity
    else:
        assert "persisted validation failed" in output
        assert original(state).revision == "new"


# These CLI regressions isolate external state while retaining real registration,
# skill planning, cache validation, and sidecar orchestration.
def _inspection_environment(monkeypatch, tmp_path, *, recorded=True, skills_current=False):
    from booley.config import editor
    from booley.runtime import session_refresh

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    source, path = _inspection_installation(monkeypatch, tmp_path, recorded)
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kwargs: False)
    monkeypatch.setattr(editor, "resolve_editor_management_command", lambda: "/fake/code")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/fake/" + name)
    if skills_current:
        bootstrap.reconcile_host_skills(source, dry_run=False, allow_retarget=True)
    calls = []
    run = _inspection_subprocess(calls)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    base = LifecycleResult("base", None, Status.STALE)
    monkeypatch.setattr(bootstrap, "reconcile_images", lambda *_a, **_kw: base)
    cache = _inspection_cache(monkeypatch, tmp_path)

    docker = _InspectionDocker()
    monkeypatch.setattr(bootstrap.host_sidecars, "_docker_adapter", lambda: docker)
    return SimpleNamespace(
        home=home,
        identity_path=path,
        cache=cache,
        docker=docker,
        run=run,
        calls=calls,
        source=source,
    )


def _inspection_installation(monkeypatch, tmp_path, recorded):
    import json
    from dataclasses import asdict

    from booley.runtime import host_install

    source = tmp_path / "package" / "skills"
    (source / "example").mkdir(parents=True)
    (source / "example" / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    identity = host_install.HostInstallationIdentity(
        1,
        "/opt/bin/booley",
        "/opt/bin/python",
        "/opt/site-packages",
        "1.0",
        "abc123",
        "f" * 64,
    )
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    monkeypatch.setattr(bootstrap, "skills_dir", lambda: source)
    monkeypatch.setattr(bootstrap, "active_qa_source_root", lambda: None)
    monkeypatch.setattr(bootstrap, "load_host_policy", lambda **_kwargs: SandboxHostPolicy())
    path = host_install.host_installation_path()
    if recorded:
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_text(json.dumps(asdict(identity)), encoding="utf-8")
    return source, path


def _inspection_cache(monkeypatch, tmp_path):
    import hashlib

    from booley.harness import nangate_pdk

    cache = tmp_path / "cache"
    cache.mkdir()
    member = nangate_pdk.PinnedFile("member", "member", hashlib.sha256(b"ok").hexdigest())
    monkeypatch.setattr(nangate_pdk, "FILES", (member,))
    monkeypatch.setattr(nangate_pdk, "cache_root", lambda: cache)
    monkeypatch.setattr(nangate_pdk, "_license_bytes", lambda: b"license")

    return cache


def _inspection_subprocess(calls):
    def run(command, **kwargs):
        assert kwargs.get("timeout")
        assert not any(arg in command for arg in ("--install-extension", "build", "pull", "run"))
        calls.append(command)
        output = "git version 2.48.0" if command[0].endswith("git") else "available"
        if "--list-extensions" in command:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    return run


class _InspectionDocker:
    def __init__(self):
        self.failure = None
        self.calls = []

    def run(self, args, *, timeout=30):
        assert timeout
        assert "inspect" in args, args
        self.calls.append(args)
        resource = (
            "proxy-image"
            if args[2] == bootstrap.host_sidecars.legacy.PROXY_IMAGE
            else "reaper-image"
            if args[2] == bootstrap.host_sidecars.legacy.REAPER_IMAGE
            else "network"
            if args[0] == "network"
            else "proxy"
            if args[2] == bootstrap.host_sidecars.legacy.PROXY_CONTAINER
            else "reaper"
        )
        if resource == self.failure:
            raise bootstrap.host_sidecars.SidecarError("access denied " + resource)
        return subprocess.CompletedProcess(args, 1, "", "No such object")


def _run_inspection(capsys):
    status = bootstrap_cli.run_bootstrap(SimpleNamespace(check_only=True))
    output = capsys.readouterr().out
    return status, output


def _assert_complete(output):
    for resource in (
        "host-config",
        "git",
        "docker",
        "vscode",
        "vscode-dev-containers",
        "skills",
        "nangate45",
        "base-image",
        "proxy-image",
        "reaper-image",
        "network",
        "proxy",
        "reaper",
        "host-install",
    ):
        assert f" {resource}:" in output, f"missing resource {resource}"


def _tree_snapshot(root):
    return {
        str(p.relative_to(root)): (
            stat.S_IMODE(p.lstat().st_mode),
            _snapshot_bytes(p) if p.is_file() and not p.is_symlink() else None,
        )
        for p in root.rglob("*")
    }


def _snapshot_bytes(path, reader=Path.open):
    # Observe disk contents even when a test injects an application read failure.
    with reader(path, "rb") as stream:
        return stream.read()


def test_check_only_fresh_config_reports_all_pending_without_writes(monkeypatch, tmp_path, capsys):
    _inspection_environment(monkeypatch, tmp_path, recorded=False)
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    assert status == 1, "fresh eligible wheel must be pending"
    _assert_complete(output)
    for line in output.splitlines():
        if "[!!]" in line and "qa-skills:" not in line:
            assert "booley bootstrap" in line
    assert _tree_snapshot(tmp_path) == before


def _deny_cache_read(monkeypatch, environment):
    from booley.harness import nangate_pdk

    member = environment.cache / "member"
    member.write_bytes(b"ok")
    (environment.cache / nangate_pdk.LICENSE_FILENAME).write_bytes(b"license")
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == member:
            raise PermissionError("access denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)


@pytest.mark.parametrize(
    "resource",
    ["host-install", "git", "vscode-dev-containers", "skills", "nangate45", "base-image"],
)
def test_check_only_continues_after_resource_error(monkeypatch, tmp_path, capsys, resource):
    environment = _inspection_environment(monkeypatch, tmp_path)
    if resource == "host-install":
        environment.identity_path.write_text("{invalid", encoding="utf-8")
    elif resource in {"git", "vscode-dev-containers"}:

        def failed(command, **kwargs):
            if (resource == "git" and command[0].endswith("git")) or (
                resource == "vscode-dev-containers" and "--list-extensions" in command
            ):
                return subprocess.CompletedProcess(command, 1, "", "probe denied")
            return environment.run(command, **kwargs)

        monkeypatch.setattr(bootstrap.subprocess, "run", failed)
    elif resource == "skills":
        target = environment.home / ".agents" / "skills"
        target.mkdir(parents=True)
        (target / ".booley-skill-links.json").write_text("{invalid", encoding="utf-8")
    elif resource == "nangate45":
        _deny_cache_read(monkeypatch, environment)
    else:

        def broken(*_args, **_kwargs):
            raise bootstrap.ImageLifecycleError("cannot inspect base image: access denied")

        monkeypatch.setattr(bootstrap, "reconcile_images", broken)
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    assert _tree_snapshot(tmp_path) == before
    if resource == "nangate45":
        assert "[XX] nangate45:" in output, "cache access failure must be ERROR"
    _assert_complete(output)
    assert status == 2
    line = next(line for line in output.splitlines() if f" {resource}:" in line)
    assert any(word in line.lower() for word in ("restore", "install", "access", "repair", "fix"))


def test_check_only_invalid_policy_still_inspects_independent_resources(
    monkeypatch, tmp_path, capsys
):
    _inspection_environment(monkeypatch, tmp_path)

    def invalid(**_kwargs):
        raise HostConfigError(tmp_path / "config.toml", "sandbox.max_sessions", "invalid value")

    monkeypatch.setattr(bootstrap, "load_host_policy", invalid)
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    for resource in ("proxy", "reaper"):
        assert "policy" in next(line for line in output.splitlines() if f" {resource}:" in line)


def test_check_only_prerequisite_error_lists_pending_with_remedies(monkeypatch, tmp_path, capsys):
    environment = _inspection_environment(monkeypatch, tmp_path)

    def failed(command, **kwargs):
        if command[-1] == "info":
            return subprocess.CompletedProcess(command, 1, "", "daemon access denied")
        return environment.run(command, **kwargs)

    monkeypatch.setattr(bootstrap.subprocess, "run", failed)
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    for line in output.splitlines():
        if "[!!]" in line:
            assert "booley bootstrap" in line
    assert "daemon" in output and "access" in output
    assert not environment.docker.calls


def test_check_only_cache_permission_error_is_not_pending(monkeypatch, tmp_path, capsys):
    environment = _inspection_environment(monkeypatch, tmp_path)
    _deny_cache_read(monkeypatch, environment)
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    assert _tree_snapshot(tmp_path) == before
    assert "[XX] nangate45:" in output, "cache access failure must be ERROR"
    assert status == 2
    _assert_complete(output)
    assert str(environment.cache / "member") in output
    assert "access" in output


def test_check_only_pending_recovery_lists_resources_without_recovering(
    monkeypatch, tmp_path, capsys
):
    from booley.runtime import session_refresh

    _inspection_environment(monkeypatch, tmp_path)

    def pending(*, read_only):
        assert read_only, "inspection must not attempt recovery"
        return True

    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", pending)
    status, output = _run_inspection(capsys)
    assert status == 1, "pending recovery must be pending"
    _assert_complete(output)
    assert "attempt recovery" in output


def test_check_only_recovery_error_preserves_other_findings(monkeypatch, tmp_path, capsys):
    from booley.runtime import session_refresh, session_runtime

    _inspection_environment(monkeypatch, tmp_path)

    def failed(*, read_only):
        assert read_only
        raise session_runtime.SessionError("unsafe journal; restore permissions")

    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", failed)
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    assert _tree_snapshot(tmp_path) == before
    _assert_complete(output)
    assert status == 2
    assert "restore permissions" in output


def test_complete_inspection_never_takes_lock_or_registers(monkeypatch, tmp_path, capsys):
    _inspection_environment(monkeypatch, tmp_path, recorded=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("read-only inspection attempted mutation")

    monkeypatch.setattr(bootstrap_cli, "host_lifecycle_lock", forbidden)
    monkeypatch.setattr(bootstrap_cli, "register_host_installation", forbidden)
    monkeypatch.setattr(bootstrap.nangate_pdk, "fetch", forbidden)
    monkeypatch.setattr(bootstrap.nangate_pdk, "secure_config_dir_for_cache", forbidden)
    status, _output = _run_inspection(capsys)
    assert status == 1


def test_complete_inspection_missing_docker_does_not_execute_it(monkeypatch, tmp_path, capsys):
    environment = _inspection_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: None if name == "docker" else "/fake/" + name
    )
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    assert environment.docker.calls == []
    assert all(not command[0].endswith("docker") for command in environment.calls)
    assert "PATH" in output


def test_complete_inspection_invalid_candidate_preserves_qa_warning(monkeypatch, tmp_path, capsys):
    from booley.runtime import host_install

    _inspection_environment(monkeypatch, tmp_path)

    def invalid(_source):
        raise host_install.HostInstallationError("invalid build; reinstall Booley")

    monkeypatch.setattr(host_install, "current_host_installation", invalid)
    monkeypatch.setattr(
        bootstrap, "active_qa_source_root", Mock(side_effect=OSError("checkout is gone"))
    )
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    assert "[!!] qa-skills: checkout is gone" in output
    assert "--without-qa-skills" in output


def test_complete_inspection_all_current_and_warning_only(monkeypatch, tmp_path):
    from booley.harness import nangate_pdk

    environment = _inspection_environment(monkeypatch, tmp_path, skills_current=True)
    (environment.cache / "member").write_bytes(b"ok")
    (environment.cache / nangate_pdk.LICENSE_FILENAME).write_bytes(b"license")

    def current_extensions(command, **kwargs):
        if "--list-extensions" in command:
            return subprocess.CompletedProcess(
                command, 0, bootstrap.DEV_CONTAINERS_EXTENSION_ID, ""
            )
        return environment.run(command, **kwargs)

    monkeypatch.setattr(bootstrap.subprocess, "run", current_extensions)
    result = bootstrap.inspect_bootstrap(include_images=False)
    assert result.exit_status == 0
    assert not any("image" in finding.resource for finding in result.findings)
    monkeypatch.setattr(
        bootstrap,
        "_host_agent_cli_finding",
        lambda: bootstrap.BootstrapFinding(
            "host-agent-cli", bootstrap.BootstrapState.WARNING, "optional CLI missing"
        ),
    )
    assert bootstrap.inspect_bootstrap(include_images=False).exit_status == 0


def test_check_only_cache_directory_collision_is_error(monkeypatch, tmp_path, capsys):
    environment = _inspection_environment(monkeypatch, tmp_path)
    (environment.cache / "member").mkdir()
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    assert "[XX] nangate45:" in output, "cache directory collision must be ERROR"
    assert status == 2
    _assert_complete(output)
    assert str(environment.cache / "member") in output
    assert "remove or rename" in output
    assert _tree_snapshot(tmp_path) == before


def test_check_only_invalid_utf8_policy_preserves_independent_findings(
    monkeypatch, tmp_path, capsys
):
    from booley.config import host_config

    _inspection_environment(monkeypatch, tmp_path)
    path = host_config.host_config_path()
    path.write_bytes(b"\xff")
    monkeypatch.setattr(
        bootstrap,
        "load_host_policy",
        lambda **kwargs: host_config.load_host_policy(path, **kwargs),
    )
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert "[XX] host-config:" in output
    assert status == 2
    assert str(path) in output
    assert "UTF-8" in output
    assert _tree_snapshot(tmp_path) == before


def test_check_only_preserves_pending_cache_license_notice(monkeypatch, tmp_path, capsys):
    _inspection_environment(monkeypatch, tmp_path)
    _status, output = _run_inspection(capsys)
    assert "non-commercial use" in output, "pending cache must preserve license notice"
    assert "Terms: LicenseRef-Nangate-OCL-1.0" in output


def test_check_only_ineligible_install_names_canonical_command(monkeypatch, tmp_path, capsys):
    from booley.runtime import host_install

    _inspection_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: "source checkout")
    status, output = _run_inspection(capsys)
    line = next(line for line in output.splitlines() if " host-install:" in line)
    assert "run the canonical host `booley bootstrap`" in line, "use the canonical host remedy"
    assert "reinstall" not in line
    assert status == 2
    assert "[XX] skills:" in output


def test_check_only_policy_blocker_has_only_its_applicable_remedy(monkeypatch, tmp_path, capsys):
    _inspection_environment(monkeypatch, tmp_path)

    def invalid(**_kwargs):
        raise HostConfigError(tmp_path / "config.toml", "", "invalid value")

    monkeypatch.setattr(bootstrap, "load_host_policy", invalid)
    _status, output = _run_inspection(capsys)
    line = next(line for line in output.splitlines() if " proxy:" in line)
    assert "foreign Docker resource" not in line, (
        "policy blocker must not recommend Docker removal"
    )
    assert "repair host config" in line


def test_check_only_unsearchable_policy_preserves_independent_findings(
    monkeypatch, tmp_path, capsys
):
    from booley.config import host_config

    _inspection_environment(monkeypatch, tmp_path)
    path = host_config.host_config_path()
    original = Path.exists

    def denied(candidate):
        if candidate == path:
            raise PermissionError("policy directory is not searchable")
        return original(candidate)

    monkeypatch.setattr(Path, "exists", denied)
    monkeypatch.setattr(bootstrap, "load_host_policy", host_config.load_host_policy)
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    assert str(path) in output
    assert "policy directory is not searchable" in output
