"""Migrated runtime diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.devcontainer_spec_support import build_spec
from tests.diagnostic_helpers import _isolate_runtime_files, _issued_runtime_state, _Rec
from tests.docker_cli_guard import (
    route_bounded_captures_through_subprocess_run,
    route_docker_helper_through_subprocess_run,
)
from tests.harness import test_session_runtime as resolution_fixtures
from tests.harness.session_docker_fake import SandboxDocker, completed, route_docker_commands
from tests.sharing_error_support import hold_opens
from tests.subprocess_budget import run_bounded_child

from booley.config.eda import EdaConfig
from booley.core.host_shell import quote_arg
from booley.eda.provisioning.licensing.flexnet_docker import (
    RelayDockerError,
    resources_for_session,
)
from booley.harness import doctor
from booley.runtime import (
    auth_token,
    inspection,
    project_image,
    runtime_context,
    session_admission,
    session_runtime,
)
from booley.runtime import devcontainer as dc
from booley.runtime import host_docker as idk
from booley.runtime import session_issuance as runtime_spec
from booley.runtime.sandbox_apply import Attachment


@pytest.fixture(autouse=True)
def _docker_helper_uses_subprocess_run(monkeypatch):
    """This module fakes the Docker CLI and Git through ``subprocess.run``."""
    route_docker_helper_through_subprocess_run(monkeypatch)
    route_bounded_captures_through_subprocess_run(monkeypatch)


@pytest.fixture(autouse=True)
def _isolate_active_inventory(monkeypatch):
    # Existing issuance tests independently inspect retained/stopped resources.
    # Active selection has its own real-inventory regressions below.
    monkeypatch.setattr(session_admission, "running_project_sandboxes", lambda *_a, **_kw: ())


@pytest.fixture(autouse=True)
def _host_side(monkeypatch, fake_sandbox_attachment):
    """Inspect as the host does, with an unknown Sandbox owner; in-runtime tests override it."""
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)


project_sandbox_resolution = resolution_fixtures.project_sandbox_resolution
workspace = resolution_fixtures.workspace


def _issuance(root: Path, docker_exe: str | None = "docker"):
    request = inspection.RuntimeInspectionRequest(root, "booley-sandbox", docker_exe)
    return inspection.inspect_runtime(request).issuance


def _messages(report, severity: str) -> list[str]:
    return [item.message for item in report.findings if item.severity == severity]


def _write_spec(root: Path, spec: dict) -> None:
    path = root / ".devcontainer" / "devcontainer.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(spec), encoding="utf-8")


def test_host_doctor_rejects_unissued_session_spec(tmp_path) -> None:
    _write_spec(tmp_path, {})
    # The real validator refuses a spec Booley never issued; its reason reaches the FAIL.
    (message,) = _messages(_issuance(tmp_path), "fail")
    assert "host issuance is invalid" in message
    assert "Sandbox issuance was withdrawn" in message


@pytest.fixture
def live_sandbox(tmp_path, monkeypatch):
    """A running, issued Sandbox `runtime-1` served by the shared Docker fake."""
    issuance, spec, labels, state = _issued_runtime_state(tmp_path)
    _write_spec(tmp_path, spec)
    # The issuance validator needs a Booley-issued spec; the stub hands back the issuance.
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
    docker = route_docker_commands(monkeypatch, SandboxDocker())
    labels["booley.role"] = "interactive"  # the inventory lists Sandboxes by this label
    state.update({"Id": "id-runtime-1", "Name": "/runtime-1", "State": {"Running": True}})
    docker.containers["runtime-1"] = state
    return SimpleNamespace(
        docker=docker, issuance=issuance, spec=spec, labels=labels, state=state, root=tmp_path
    )


@pytest.mark.parametrize(
    ("drift", "mutate"),
    [
        ("image", lambda s: s.update(Image="sha256:" + "d" * 64)),
        ("workspace-mount", lambda s: s["Mounts"][0].update(Source="/wrong-workspace")),
        ("cap-add", lambda s: s["HostConfig"].update(CapAdd=["SYS_ADMIN"])),
        ("privileged", lambda s: s["HostConfig"].update(Privileged=True)),
        ("host-pid", lambda s: s["HostConfig"].update(PidMode="host")),
        ("host-ipc", lambda s: s["HostConfig"].update(IpcMode="host")),
        ("device", lambda s: s["HostConfig"].update(Devices=[{"PathOnHost": "/dev/kvm"}])),
        (
            "published-port",
            lambda s: s["HostConfig"].update(PortBindings={"22/tcp": [{"HostPort": "2222"}]}),
        ),
        (
            "extra-security-option",
            lambda s: s["HostConfig"]["SecurityOpt"].append("label=disable"),
        ),
        (
            "unreported-state",
            lambda s: (s.pop("State"), s["HostConfig"].update(Privileged=True)),
        ),
    ],
)
def test_host_doctor_rejects_full_live_runtime_state_drift(live_sandbox, drift, mutate) -> None:
    mutate(live_sandbox.state)
    failures = _messages(_issuance(live_sandbox.root), "fail")
    assert "live Sandbox state differs from current host issuance" in failures, drift


def test_host_doctor_names_rebuild_repair_for_running_old_vscode(live_sandbox) -> None:
    state, root = live_sandbox.state, live_sandbox.root
    state["Image"] = "sha256:" + "d" * 64
    labels = live_sandbox.labels
    labels.update(
        {
            "devcontainer.local_folder": str(root),
            "devcontainer.config_file": str(root / ".devcontainer" / "devcontainer.json"),
        }
    )
    state["Config"]["Labels"] = labels
    # Current, pinned Booley Directory mounts: the editor is stale, not outdated.
    state["Mounts"] += [
        {"Destination": target, "Source": str(root / ".booley"), "Type": "bind", "RW": True}
        for target in ("/booley", "/work/.booley")
    ]
    fixes = [item.fix for item in _issuance(root).findings if item.severity == "fail"]
    assert any(
        fix.startswith("for VS Code Sandbox(s) 'runtime-1', on the host run `booley init`, then ")
        and "Dev Containers: Rebuild Container" in fix
        and "stop" not in fix
        for fix in fixes
    )
    inventory = next(argv for argv in live_sandbox.docker.calls if argv[1:3] == ["ps", "-aq"])
    assert inventory[-2:] == ["--format", "{{.Names}}"]


def test_host_doctor_accepts_vscode_managed_runtime_state(live_sandbox) -> None:
    state, spec = live_sandbox.state, live_sandbox.spec
    spec["remoteEnv"] = {"BOOLEY_DIR": "/booley"}
    _write_spec(live_sandbox.root, spec)
    live_sandbox.labels["devcontainer.local_folder"] = str(live_sandbox.root)
    state["Config"]["Labels"] = live_sandbox.labels
    state["Mounts"].extend(
        [
            {"Destination": "/vscode", "Name": "vscode", "Type": "volume", "RW": True},
            {
                "Destination": "/tmp/vscode-wayland-1234-abcd.sock",
                "Source": "/run/user/1000/wayland-0",
                "Type": "bind",
                "RW": True,
            },
        ]
    )
    report = _issuance(live_sandbox.root)
    assert not _messages(report, "fail")
    assert any("state matches" in message for message in _messages(report, "pass"))


def test_in_runtime_doctor_does_not_require_vivado_for_disabled_fpga(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    _isolate_runtime_files(monkeypatch)
    request = inspection.RuntimeInspectionRequest(
        tmp_path, "image", None, eda={"vivado": EdaConfig("vivado", "host")}, fpga_enabled=False
    )
    report = inspection.inspect_runtime(request).issuance
    assert not _messages(report, "fail")
    assert any(
        "no active host-mounted commercial EDA request" in message
        for message in _messages(report, "pass")
    )


@pytest.fixture
def retained_docker(tmp_path, monkeypatch):
    """Docker lists the Project's own state volume, an orphan volume and keeper images."""
    docker = route_docker_commands(monkeypatch, SandboxDocker())

    def listing(volumes: list[str] | None, images: list[str] | str):
        if volumes is None:
            docker.script(("volume", "ls"), completed(1, stderr="daemon unavailable"))
        else:
            docker.script(("volume", "ls"), completed(stdout="".join(f"{v}\n" for v in volumes)))
        if isinstance(images, str):
            docker.script(("image", "ls"), completed(1, stderr=images))
        else:
            docker.script(("image", "ls"), completed(stdout="".join(f"{i}\n" for i in images)))

    return listing


def test_retained_resources_skip_without_runtime(tmp_path) -> None:
    report = inspection.inspect_retained_resources(tmp_path, None)
    assert {item.severity for item in report.findings} == {"skip"}


def test_retained_resources_report_nothing_retained(tmp_path, retained_docker) -> None:
    retained_docker([], [])
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    assert [item.message for item in report.findings] == [
        "no persistent interactive state volumes",
        "no retained Sandbox Image keepers",
    ]


def test_retained_resources_treat_an_unlistable_volume_inventory_as_empty(
    tmp_path, retained_docker
) -> None:
    retained_docker(None, [])
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    assert "no persistent interactive state volumes" in _messages(report, "pass")


def test_verbose_doctor_lists_orphan_volumes(tmp_path, retained_docker, capsys) -> None:
    retained_docker(["booley-codex-state-someoldproject"], [])
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    # _Reporter: verbose detail rendering is reachable publicly only through a whole
    # Doctor run over a Project.
    doctor._Reporter.create(verbose=True).diagnostics(report)
    assert "booley-codex-state-someoldproject" in capsys.readouterr().out


def test_tracked_devcontainer_spec_fails(tmp_path) -> None:
    _write_spec(tmp_path, {})
    for args in (["init", "-q"], ["add", ".devcontainer"]):
        run_bounded_child(["git", "-C", str(tmp_path), *args], check=True)
    request = inspection.RuntimeInspectionRequest(tmp_path, "booley-sandbox", None)
    (failure,) = _messages(inspection.inspect_runtime(request).configuration, "fail")
    assert ".devcontainer/ is tracked by git" in failure


def test_retained_resources_recognize_this_projects_resources(tmp_path, retained_docker) -> None:
    mine = f"booley-claude-state-{dc.canonical_project_id(tmp_path)}"
    retained_docker([mine], [runtime_spec.keeper_image(tmp_path)])
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    assert {item.severity for item in report.findings} == {"pass"}
    assert any(mine in message for message in _messages(report, "pass"))
    assert any("retained for this Project" in message for message in _messages(report, "pass"))


def test_retained_resources_flag_other_projects_as_prunable(tmp_path, retained_docker) -> None:
    mine = f"booley-claude-state-{dc.canonical_project_id(tmp_path)}"
    orphans = ["booley-codex-state-someoldproject", "booley-claude-state-anotherproject"]
    other_keeper = runtime_spec.keeper_image(tmp_path / "old-project")
    retained_docker([mine, *orphans], [runtime_spec.keeper_image(tmp_path), other_keeper])
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    notes = _messages(report, "note")
    assert len(notes) == 2
    assert "2 interactive state volume(s) from other projects" in notes[0]
    assert "docker volume rm" in notes[0]
    assert "1 issued image keeper(s)" in notes[1]
    # Verbose Doctor lists these rows after the finding that introduces them.
    assert sorted(detail.message.strip() for detail in report.details) == sorted(
        [*orphans, other_keeper]
    )
    assert all(detail.after_finding is not None for detail in report.details)


def test_keeper_listing_failure_is_a_doctor_finding(tmp_path, retained_docker) -> None:
    """A Docker failure while listing keepers is a FAIL, never "no retained keepers"."""
    retained_docker([], "Cannot connect to the Docker daemon")
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    (failure,) = [item for item in report.findings if item.severity == "fail"]
    assert "could not list issued Sandbox Image keepers" in failure.message
    assert "Cannot connect to the Docker daemon" in failure.message
    assert "booley doctor" in failure.fix
    assert not any("no retained Sandbox Image keepers" in item.message for item in report.findings)


def _configuration(
    root: Path,
    monkeypatch,
    *,
    image: str | None = None,
    declared_provider: str | None = None,
    declared_clis: tuple[str, ...] | None = None,
    docker_exe: str | None = None,
) -> _Rec:
    """Inspect the spec on disk, with that same spec standing in for what init would write.

    The preconditions tested here (image, provider, CLI set) run before it is consulted.
    """
    # Isolate from the developer machine's own `booley auth` store, which the
    # credential-policy precondition reads.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-isolated"))
    on_disk = dc.devcontainer_path(root)
    # idk.image_id resolves the image through Docker, which these Docker-less cases lack;
    # the tag itself stands in for its ID. Git inspection of the temporary directory is real
    # and reports no tracked spec.
    monkeypatch.setattr(idk, "image_id", lambda image: image)
    rec = _Rec()
    request = inspection.RuntimeInspectionRequest(
        root,
        image or dc.SANDBOX_IMAGE,
        docker_exe,
        declared_provider=declared_provider,
        **({"declared_clis": declared_clis} if declared_clis else {}),
        expected_spec=lambda _image: json.loads(on_disk.read_text(encoding="utf-8")),
    )
    rec.d(inspection.inspect_runtime(request).configuration)
    return rec


_CUSTOM_IMAGE = "openc910-booley-sandbox:latest"
_OLD_ID = "sha256:" + "a" * 64


def test_image_comparison_uses_shared_dispatch(tmp_path, monkeypatch):
    dc.write_devcontainer(tmp_path, build_spec(dc.APP_CLAUDE, image=_OLD_ID))
    docker = route_docker_commands(monkeypatch, SandboxDocker(), executable="custom-docker")
    rec = _configuration(tmp_path, monkeypatch, image="configured", docker_exe="custom-docker")
    assert docker.calls, "the comparison never reached Docker"
    assert {argv[0] for argv in docker.calls} == {"custom-docker"}
    assert not any(level == "warn" for level, _ in rec.events)


@pytest.mark.parametrize(
    ("spec", "kwargs"),
    [
        pytest.param(lambda: build_spec(dc.APP_CLAUDE), {}, id="fresh-or-undeclared-provider"),
        pytest.param(
            lambda: build_spec(dc.APP_CLAUDE, image=_CUSTOM_IMAGE),
            {"image": _CUSTOM_IMAGE},
            id="matching-custom-image",
        ),
        pytest.param(
            lambda: build_spec(dc.APP_CODEX),
            {"declared_provider": dc.APP_CODEX},
            id="app-matches-declared-provider",
        ),
    ],
)
def test_current_spec_passes(tmp_path, monkeypatch, spec, kwargs):
    dc.write_devcontainer(tmp_path, spec())
    assert _configuration(tmp_path, monkeypatch, **kwargs).kinds() == {"pass"}


def test_image_drift_warns_not_fails(tmp_path, monkeypatch):
    # Spec frozen on the base image while [sandbox].image now names a custom
    # project image (extra toolchain) — the openc910/Xuantie blocker shape.
    dc.write_devcontainer(tmp_path, build_spec(dc.APP_CLAUDE))
    rec = _configuration(tmp_path, monkeypatch, image=_CUSTOM_IMAGE)
    assert rec.fails() == []
    assert any(
        lvl == "warn" and _CUSTOM_IMAGE in m and "booley session refresh" in m
        for lvl, m in rec.events
    )


def test_immutable_image_pin_is_not_stale_when_resolution_unavailable(tmp_path, monkeypatch):
    # Inside an issued Sandbox Docker is intentionally absent. The
    # spec is already pinned to an immutable ID, but the configured tag
    # cannot be resolved there; string-comparing the ID to the tag would be
    # a false stale-image warning.
    dc.write_devcontainer(tmp_path, build_spec(dc.APP_CLAUDE, image=_OLD_ID))
    # The comparison needs a Docker image store; only its verdict matters here.
    monkeypatch.setattr(
        idk,
        "compare_issued_selection",
        lambda *_args, **_kwargs: inspection.image_identity.Comparison(
            inspection.image_identity.Status.UNKNOWN
        ),
    )
    rec = _configuration(tmp_path, monkeypatch, image=project_image.project_image_name(tmp_path))
    assert rec.fails() == []
    assert not any(level == "warn" for level, _ in rec.events)
    assert any(
        level == "note" and "host `booley doctor` is authoritative" in message
        for level, message in rec.events
    )


def test_same_source_immutable_image_tag_move_passes(tmp_path, monkeypatch):
    dc.write_devcontainer(tmp_path, build_spec(dc.APP_CLAUDE, image=_OLD_ID))
    # The comparison needs a Docker image store; only its verdict matters here.
    monkeypatch.setattr(
        idk,
        "compare_issued_selection",
        lambda *_args, **_kwargs: inspection.image_identity.Comparison(
            inspection.image_identity.Status.MATCH
        ),
    )
    rec = _configuration(tmp_path, monkeypatch, image=project_image.project_image_name(tmp_path))
    assert rec.fails() == []
    assert not any(level == "warn" for level, _message in rec.events)


@pytest.mark.parametrize(
    ("comparator", "image"),
    [
        ("compare_issued_selection", "managed"),
        ("compare_issued_reference", "custom-sandbox"),
    ],
)
def test_changed_image_warns_stale(tmp_path, monkeypatch, comparator, image):
    dc.write_devcontainer(tmp_path, build_spec(dc.APP_CLAUDE, image=_OLD_ID))
    mismatch = inspection.image_identity.Comparison(inspection.image_identity.Status.MISMATCH)
    # idk's comparators are patched because they need a live Docker image store; only
    # the one the image kind selects answers MISMATCH, so a swapped dispatch finds no warn.
    monkeypatch.setattr(idk, comparator, lambda *_a, **_kw: mismatch)
    selected = project_image.project_image_name(tmp_path) if image == "managed" else image
    rec = _configuration(tmp_path, monkeypatch, image=selected)
    assert any(level == "warn" and "stale" in message for level, message in rec.events), comparator


def test_agent_app_drift_fails(tmp_path, monkeypatch):
    # The picorv32 shape, hit live 2026-07-27: the project switched to
    # `[agent] provider = "codex"` long after seeding, so the untracked spec
    # still said claude. incontainer_register then wrote the Booley MCP
    # entry into ~/.claude.json while the Codex session — the only agent
    # actually running — saw no Booley MCP tools at all.
    spec = build_spec(dc.APP_CLAUDE)
    # A mismatched spec still mounts a volume for the app it *names*, so the
    # persistence check would pass and hide the real problem: the app drift
    # must be what the user is told to fix.
    assert dc.spec_volume_targets(spec)
    dc.write_devcontainer(tmp_path, spec)
    rec = _configuration(tmp_path, monkeypatch, declared_provider=dc.APP_CODEX)
    assert rec.fails() and all("BOOLEY_AGENT_APP" in m and "codex" in m for m in rec.fails())
    assert not any(lvl == "warn" for lvl, _ in rec.events)


def _bogus_cli_spec():
    spec = build_spec(dc.APP_CLAUDE)
    spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = "claude=subscription,bogus=subscription"
    return spec


@pytest.mark.parametrize(
    ("spec", "kwargs", "fragments"),
    [
        pytest.param(
            lambda: build_spec(dc.APP_CLAUDE),
            {"declared_clis": ("claude", "codex")},
            ["['claude']", "['claude', 'codex']"],
            id="other-cli-set",
        ),
        pytest.param(
            lambda: build_spec(dc.APP_NONE),
            {"declared_clis": ("claude", "codex")},
            ["BOOLEY_AGENT_APP 'none'"],
            id="none-spec",
        ),
        pytest.param(_bogus_cli_spec, {}, ["record is malformed"], id="malformed-record"),
    ],
)
def test_cli_record_failures_name_the_problem(tmp_path, monkeypatch, spec, kwargs, fragments):
    dc.write_devcontainer(tmp_path, spec())
    rec = _configuration(tmp_path, monkeypatch, declared_provider="claude", **kwargs)
    assert any(all(fragment in m for fragment in fragments) for m in rec.fails())
    assert not any(lvl == "warn" for lvl, _ in rec.events)


def test_handwritten_spec_primary_auth_auto_is_not_malformed(tmp_path, monkeypatch):
    # A hand-written spec has no issued record: ``BOOLEY_PRIMARY_AUTH=auto`` is the
    # env-only visitor mode, rebuilt byte-identically, never a retired record.
    spec = build_spec(dc.APP_CLAUDE, policy=auth_token.VISITOR_AUTH)
    spec["remoteEnv"].pop("BOOLEY_AGENT_CLIS", None)
    assert spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] == "auto"
    assert dc.spec_cli_policies(spec) == {"claude": auth_token.VISITOR_AUTH}
    rebuilt = dc.sandbox_visible_expectation(spec)
    assert rebuilt["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] == "auto"
    assert dc.sandbox_visible(rebuilt) == dc.sandbox_visible(spec)
    dc.write_devcontainer(tmp_path, spec)

    rec = _configuration(tmp_path, monkeypatch, declared_provider="claude")

    assert not [m for m in rec.fails() if "malformed" in m]


def test_spec_for_allowed_cli_set_raises_no_cli_set_failure(tmp_path, monkeypatch):
    # Either listed CLI may be the registered one: the Sandbox seeds both. (The
    # rebuild uses the [agent] default, so a non-default app is drift, not a FAIL.)
    spec = build_spec(dc.APP_CODEX)
    spec["name"] = "Booley Interactive (claude, codex)"
    spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = "claude=subscription,codex=subscription"
    dc.write_devcontainer(tmp_path, spec)
    rec = _configuration(
        tmp_path, monkeypatch, declared_provider="claude", declared_clis=("claude", "codex")
    )
    assert rec.fails() == []


def test_missing_spec_warns_run_init(tmp_path, monkeypatch):
    rec = _configuration(tmp_path, monkeypatch)
    assert any("no .devcontainer" in m for _, m in rec.events)


def _resolved_issuance(fixture, workspace, monkeypatch):
    """Inspect the host issuance the shared Project Sandbox resolution fixture describes."""
    # The fixture's issuance is a bare namespace; the public path also reads its image.
    fixture.issuance.image = fixture.spec["image"]
    _write_spec(workspace, fixture.spec)
    route_docker_commands(monkeypatch, SandboxDocker())
    return _issuance_for(workspace, fixture.spec["image"])


def _issuance_for(root: Path, image: str):
    request = inspection.RuntimeInspectionRequest(root, image, "docker")
    return inspection.inspect_runtime(request).issuance


def _duplicate_editor(fixture) -> None:
    other = json.loads(json.dumps(fixture.state))
    other["Id"], other["Name"] = "b" * 64, "/second-editor"
    fixture.documents[other["Id"]] = other


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(lambda f: None, None, id="none"),
        pytest.param(_duplicate_editor, "multiple running Sandboxes", id="duplicate"),
        pytest.param(
            lambda f: f.state["Config"].update(User="root"),
            "active Sandbox differs from current host issuance",
            id="user-root",
        ),
        pytest.param(
            lambda f: f.state["Config"]["Labels"].pop("booley.project-id"),
            "identity disagrees",
            id="no-project-id",
        ),
    ],
)
def test_host_inspection_resolves_project_sandbox(
    workspace, project_sandbox_resolution, monkeypatch, change, expected
):
    fixture = project_sandbox_resolution
    change(fixture)
    messages = _messages(_resolved_issuance(fixture, workspace, monkeypatch), "fail")
    if expected is None:
        assert not any(
            "multiple running" in m or "differs" in m or "identity" in m for m in messages
        )
    else:
        assert any(expected in message for message in messages)


def test_inspection_selects_drift_remedy_once(tmp_path, monkeypatch):
    remedy = Mock(return_value="run `booley session refresh` on the host")
    monkeypatch.setattr(session_runtime, "sandbox_remedy", remedy)
    # No devcontainer.json: the configuration warns and the issuance fails, both with the remedy.
    request = inspection.RuntimeInspectionRequest(tmp_path, "booley-sandbox", None)
    result = inspection.inspect_runtime(request)
    findings = result.configuration.findings + result.issuance.findings
    assert len(findings) == 2
    for item in findings:
        assert f"{item.message} {item.fix}".count("booley session refresh") == 1
    remedy.assert_called_once_with(tmp_path)


@pytest.mark.parametrize(
    ("skip", "held", "part", "absent", "present", "attempts"),
    [
        pytest.param(
            0, 1, "configuration", "does not parse", None, 3, id="transient-configuration"
        ),
        pytest.param(1, 1, "issuance", "cannot access", None, 3, id="transient-issuance"),
        pytest.param(0, 99, "issuance", None, "cannot access", 4, id="lasting"),
    ],
)
def test_spec_sharing_errors_are_retried(
    tmp_path, monkeypatch, skip, held, part, absent, present, attempts
):
    _write_spec(tmp_path, {})
    spec = tmp_path / ".devcontainer" / "devcontainer.json"
    opens = hold_opens(monkeypatch, {spec: held}, skip=skip)
    request = inspection.RuntimeInspectionRequest(tmp_path, "booley-sandbox", None)
    messages = [
        item.message for item in getattr(inspection.inspect_runtime(request), part).findings
    ]
    assert opens[spec] == attempts
    if absent:
        assert not any(absent in message for message in messages)
    if present:
        assert any("host issuance is invalid" in m and present in m for m in messages)


_RELAY_HEADLESS = "run `booley session down`, then `booley session up --rebuild` on the host"
_RELAY_VSCODE = (
    f"close VS Code, then on the host run `docker stop {quote_arg('runtime-1')}` "
    f"and `docker rm {quote_arg('runtime-1')}`, "
    "run `booley session down` to remove the relay topology, "
    "and reopen the folder in the container"
)


@pytest.mark.parametrize(
    ("case", "owner", "expected"),
    [
        ("missing", "headless", _RELAY_HEADLESS),
        ("missing", "vscode", _RELAY_VSCODE),
        ("missing", None, f"in VS Code, {_RELAY_VSCODE}; headless, {_RELAY_HEADLESS}"),
        ("differs", "vscode", _RELAY_VSCODE),
        ("differs", "docker-down", f"in VS Code, {_RELAY_VSCODE}; headless, {_RELAY_HEADLESS}"),
    ],
)
def test_relay_repair_names_owner_workflow(live_sandbox, monkeypatch, case, owner, expected):
    root, docker = live_sandbox.root, live_sandbox.docker
    issuance = replace(live_sandbox.issuance, relay_image_id="sha256:relay")
    profile = SimpleNamespace(
        server_ipv4="10.0.0.1", server_hostid="h", lmgrd_port=1, vendor_port=2
    )
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
    # A licensed Sandbox needs a host EDA License Profile and a live relay that Docker validates.
    monkeypatch.setattr(runtime_spec, "requested_license", lambda _root: profile)

    def differs(*_args, **_kwargs) -> None:
        raise RelayDockerError("labels differ")

    monkeypatch.setattr(inspection, "validate_relay", differs)
    if case == "differs":
        docker.networks.add(resources_for_session(str(root.resolve())).private_network)

    def attachment(_root):
        if owner == "docker-down":
            raise session_runtime.SessionError("docker unavailable")
        return {"vscode": Attachment.VSCODE, "headless": Attachment.HEADLESS}.get(owner)

    monkeypatch.setattr(session_runtime, "sandbox_attachment", attachment)

    (failure,) = [item for item in _issuance(root).findings if "relay" in item.message]
    assert failure.fix == expected
