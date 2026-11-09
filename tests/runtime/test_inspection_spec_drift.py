"""Doctor's devcontainer drift check: diff the on-disk spec against what init would write.

Specs are seeded through init's real entry point (``init_cmd._step_interactive``) against
a disposable host home with Claude and Codex logins, then hand-edited to produce each
kind of drift. Doctor's side uses ``init_cmd.current_interactive_spec``, the same
rebuild ``booley doctor`` injects on the host.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.session_issuance_support import install_trusted_validator, stub_keeper

from booley.audit.diagnostic_results import DiagnosticFinding, Findings
from booley.config.host_config import host_config_path
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import (
    auth_token,
    dashboard_extension,
    inspection,
    project_image,
    runtime_context,
    session_runtime,
    vaporview,
)
from booley.runtime import devcontainer as dc
from booley.runtime import session_issuance as runtime_spec

_IMAGE_A = "sha256:" + "a" * 64
_IMAGE_B = "sha256:" + "b" * 64
_DRIFT_ID = "interactive.devcontainer-drift"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Project root on a disposable host: isolated home and config, logins, trusted launcher."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "BOOLEY_DIR",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_AGENT_APP",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(key, raising=False)
    logins = {
        "claude": {"claudeAiOauth": {"accessToken": "SYNTHETIC_CLAUDE_LOGIN"}},
        "codex": {"tokens": {"access_token": "SYNTHETIC_CODEX_LOGIN"}, "OPENAI_API_KEY": None},
    }
    for app, body in logins.items():
        login = auth_token.subscription_creds_path(app)
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_text(json.dumps(body), encoding="utf-8")
    validator = install_trusted_validator(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(validator.parent))
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    stub_keeper(monkeypatch)
    # No Docker: the image's Booley Directory layout probe reports the plain directory form.
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image_id: False)
    # ... and there is no Sandbox container to reconcile.
    monkeypatch.setattr(
        session_runtime,
        "plan_stopped_headless_runtime_reconciliation",
        lambda *_args, **_kwargs: session_runtime.StoppedHeadlessRuntimePlan(),
    )
    # An immutable ID resolves to itself, a keeper tag to what it was last tagged with,
    # and any other image reference to B.
    tags: dict[str, str] = {}
    monkeypatch.setattr(
        inspection.idk, "tag_image", lambda image_id, tag: tags.__setitem__(tag, image_id)
    )
    monkeypatch.setattr(
        runtime_spec,
        "_resolve_image_id",
        lambda image: image if image.startswith("sha256:") else tags.get(image, _IMAGE_B),
    )
    monkeypatch.setattr(runtime_spec, "_keeper_image_id", tags.get)
    root = tmp_path / "project"
    (root / ".booley").mkdir(parents=True)
    return root


def _set_host_egress(mode: str) -> None:
    """Write host ``[sandbox] egress`` into the test's isolated config dir."""
    path = host_config_path()
    for directory in (path.parent.parent, path.parent):
        directory.mkdir(mode=0o700, exist_ok=True)
    path.write_text(f'[sandbox]\negress = "{mode}"\n', encoding="utf-8")


def _seed(
    root: Path,
    app: str,
    *,
    egress: str = "proxy",
    image_id: str = _IMAGE_A,
    nangate: Path | None = None,
    auth: str | None = None,
) -> dict:
    """Seed the spec the way full init and ``--seed`` do; return it as written."""
    auth_line = "" if auth is None else f'auth = "{auth}"\n'
    toml = "" if app == dc.APP_NONE else f'[agent]\nprovider = "{app}"\n{auth_line}'
    (root / ".booley" / "booley.toml").write_text(toml, encoding="utf-8")
    _set_host_egress(egress)
    init_cmd._step_interactive(
        InitContext(project_root=root, force=True, verbose=False),
        nangate_pdk_root=nangate if nangate is not None else init_cmd._NANGATE_PDK_NOT_REQUESTED,
        agent_app=app,
        runtime_image_id=image_id,
    )
    return _read(root)


def _read(root: Path) -> dict:
    return json.loads(dc.devcontainer_path(root).read_text(encoding="utf-8"))


def _write(root: Path, spec: dict) -> None:
    dc.devcontainer_path(root).write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


def _inspect(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_spec: Callable[[str | None], dict] | None,
    declared_provider: str | None = None,
) -> tuple[DiagnosticFinding, ...]:
    """Doctor's drift findings for *root*; image currency is not what is tested here."""
    monkeypatch.setattr(
        inspection.idk,
        "compare_issued_image",
        lambda *_a, **_kw: inspection.image_identity.Comparison(
            inspection.image_identity.Status.MATCH
        ),
    )
    request = inspection.RuntimeInspectionRequest(
        root,
        "booley-sandbox",
        None,
        declared_provider=declared_provider,
        expected_spec=expected_spec,
    )
    report = Findings()
    inspection._check_devcontainer_spec(request, report)
    return report.report().findings


def _host(root: Path) -> Callable[[str | None], dict]:
    """The host Doctor's rebuild, as ``_run_mcp_checks`` injects it."""
    return functools.partial(init_cmd.current_interactive_spec, root)


def _drift(findings: tuple[DiagnosticFinding, ...]) -> DiagnosticFinding:
    """The one drift WARN among *findings*."""
    (found,) = [f for f in findings if f.check_id == _DRIFT_ID]
    assert found.severity.value == "warn"
    return found


def _vscode(spec: dict) -> dict:
    return spec["customizations"]["vscode"]


def test_freshly_seeded_spec_has_no_drift(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every supported agent app under both egress modes, each reseeded over the last."""
    for egress in ("proxy", "unproxied"):
        for app in (dc.APP_CLAUDE, dc.APP_CODEX, dc.APP_NONE):
            _assert_fresh_spec_has_no_drift(project, monkeypatch, app, egress)


def _assert_fresh_spec_has_no_drift(
    project: Path, monkeypatch: pytest.MonkeyPatch, app: str, egress: str
) -> None:
    seeded = _seed(project, app, egress=egress)
    assert seeded["remoteEnv"]["BOOLEY_AGENT_APP"] == app
    # The host egress mode really reached the spec, so both modes are exercised.
    assert (seeded["containerEnv"].get("BOOLEY_EGRESS") == "unproxied") == (egress == "unproxied")
    rebuilds: list[str | None] = []
    rebuild = _host(project)

    def spy(image: str | None) -> dict:
        rebuilds.append(image)
        return rebuild(image)

    findings = _inspect(project, monkeypatch, expected_spec=spy)

    assert rebuilds == [_IMAGE_A]
    assert [(f.severity.value, f.message) for f in findings] == [
        ("pass", "devcontainer.json matches the spec `booley init` would write")
    ]


@pytest.mark.parametrize(
    ("app", "policy"), [(dc.APP_CLAUDE, "api_key"), (dc.APP_CODEX, "subscription")]
)
def test_freshly_seeded_spec_with_non_default_credential_policy_has_no_drift(
    project: Path, monkeypatch: pytest.MonkeyPatch, app: str, policy: str
) -> None:
    """A policy that deliberately omits the stored token's seed mount is not drift."""
    auth_token.store_token("SYNTHETIC", app)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_KEY")
    _seed(project, app, auth=policy)

    findings = _inspect(project, monkeypatch, expected_spec=_host(project))

    assert [f.severity.value for f in findings] == ["pass"]


def test_spec_differing_only_in_image_pin_has_no_drift(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seeded under image A; the project's image reference now resolves to B.
    seeded = _seed(project, dc.APP_CLAUDE, image_id=_IMAGE_A)
    assert seeded["image"] == _IMAGE_A
    assert runtime_spec._resolve_image_id("booley-sandbox") == _IMAGE_B
    pins: list[str | None] = []
    rebuild = _host(project)

    def spy(image: str | None) -> dict:
        pins.append(image)
        return rebuild(image)

    findings = _inspect(project, monkeypatch, expected_spec=spy)

    assert pins == [_IMAGE_A]
    assert [f.severity.value for f in findings] == ["pass"]
    assert not [f for f in findings if f.check_id == _DRIFT_ID]


def test_tag_pinned_spec_shows_the_image_in_the_diff(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _seed(project, dc.APP_CLAUDE)
    spec["image"] = "booley-sandbox:legacy"
    _write(project, spec)
    pins: list[str | None] = []
    rebuild = _host(project)

    def spy(image: str | None) -> dict:
        pins.append(image)
        return rebuild(image)

    message = _drift(_inspect(project, monkeypatch, expected_spec=spy)).message

    assert pins == [None]
    assert '-image: "booley-sandbox:legacy"' in message
    assert f'+image: "{_IMAGE_B}"' in message


def _remove_state_volume(spec: dict) -> None:
    spec["mounts"] = [m for m in spec["mounts"] if "type=volume" not in m]


def _remove_token_seed(spec: dict) -> None:
    target = dc._APP_TOKEN_SEED_TARGET[dc.APP_CLAUDE]
    spec["mounts"] = [m for m in spec["mounts"] if dc._parse_mount(m).get("target") != target]


def _gallery_vaporview(spec: dict) -> None:
    extensions = _vscode(spec)["extensions"]
    extensions[extensions.index(vaporview.BUNDLED_VSIX)] = vaporview.EXTENSION_ID


def _remove_extension(entry: str) -> Callable[[dict], None]:
    def mutate(spec: dict) -> None:
        _vscode(spec)["extensions"].remove(entry)

    return mutate


def _add_extension(entry: str) -> Callable[[dict], None]:
    def mutate(spec: dict) -> None:
        _vscode(spec)["extensions"].append(entry)

    return mutate


def _set_setting(key: str, value: object) -> Callable[[dict], None]:
    def mutate(spec: dict) -> None:
        _vscode(spec)["settings"][key] = value

    return mutate


def _remove_setting(key: str) -> Callable[[dict], None]:
    def mutate(spec: dict) -> None:
        del _vscode(spec)["settings"][key]

    return mutate


def _unpatch_attach(spec: dict) -> None:
    spec["postAttachCommand"] = dc.live_preview_port_command()


_PALETTE_KEY = next(iter(vaporview.PRESENTATION_COLOR_SETTINGS))

# One case per predicate the per-feature checks used to have: how to damage a freshly
# seeded Claude spec, and the key path the drift diff must name.
_DRIFT_CASES = {
    "state-volume": (_remove_state_volume, "mounts[]"),
    "token-seed-mount": (_remove_token_seed, "mounts[]"),
    "vaporview-gallery-entry": (_gallery_vaporview, "customizations.vscode.extensions[]"),
    "booley-integration-vsix": (
        _remove_extension(dashboard_extension.VSIX_PATH),
        "customizations.vscode.extensions[]",
    ),
    "vaporview-palette": (
        _set_setting(_PALETTE_KEY, "#123456"),
        f"customizations.vscode.settings.{_PALETTE_KEY}",
    ),
    "attach-patch": (_unpatch_attach, "postAttachCommand"),
    "hdl-vsix": (_remove_extension(dc.HDL_VSIX), "customizations.vscode.extensions[]"),
    "retired-extension": (
        _add_extension("bitwisecook.tcl"),
        "customizations.vscode.extensions[]",
    ),
    "retired-file-association": (
        _set_setting("files.associations", {"*.sdc": "tcl"}),
        "customizations.vscode.settings.files.associations.*.sdc",
    ),
    "retired-extension-kind": (
        _set_setting("remote.extensionKind", {"Anthropic.claude-code": ["ui"]}),
        "customizations.vscode.settings.remote.extensionKind.Anthropic.claude-code",
    ),
    "live-preview-setting": (
        _set_setting("livePreview.portNumber", 3000),
        "customizations.vscode.settings.livePreview.portNumber",
    ),
    "python-activation": (
        _remove_setting("python-envs.terminal.autoActivationType"),
        "customizations.vscode.settings.python-envs.terminal.autoActivationType",
    ),
}


def test_drift_finding_diff_names_the_changed_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each case reseeds a fresh spec, damages it, and expects its own drift finding."""
    for case in _DRIFT_CASES:
        _assert_drift_diff_names_key(project, monkeypatch, case)


def _assert_drift_diff_names_key(
    project: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # A stored `booley auth` token makes init mount the token seed (one case damages it).
    auth_token.store_token("SYNTHETIC_STORED_TOKEN", dc.APP_CLAUDE)
    spec = _seed(project, dc.APP_CLAUDE)
    mutate, key_path = _DRIFT_CASES[case]
    mutate(spec)
    _write(project, spec)

    finding = _drift(_inspect(project, monkeypatch, expected_spec=_host(project)))

    assert "(- on disk, + expected)" in finding.message
    diff = [line.strip() for line in finding.message.splitlines()[1:]]
    assert diff
    assert all(line.startswith(("-", "+")) for line in diff)
    assert any(line[1:].startswith(key_path) for line in diff), finding.message
    assert "SYNTHETIC_STORED_TOKEN" not in finding.message


def test_missing_nangate_cache_mount_is_drift(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed(project, dc.APP_CLAUDE)  # seeded while the verified Nangate45 cache did not exist
    cache = tmp_path / "nangate-cache"
    cache.mkdir()
    monkeypatch.setattr(init_cmd.nangate_pdk, "is_ready", lambda: True)
    monkeypatch.setattr(init_cmd.nangate_pdk, "cache_root", lambda: cache)

    finding = _drift(_inspect(project, monkeypatch, expected_spec=_host(project)))

    assert f"target={init_cmd.nangate_pdk.CONTAINER_ROOT}" in finding.message
    assert "+mounts[]" in finding.message


def test_missing_state_volume_reports_lost_transcripts(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _seed(project, dc.APP_CLAUDE)
    _remove_state_volume(spec)
    _write(project, spec)

    finding = _drift(_inspect(project, monkeypatch, expected_spec=_host(project)))

    assert "no persistent volume for the agent's home-state" in finding.message
    assert "transcripts/plans are lost on every rebuild" in finding.message
    assert any(
        line.strip().startswith("+mounts[]") and "type=volume" in line
        for line in finding.message.splitlines()
    )


def test_long_diff_is_capped(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _seed(project, dc.APP_CLAUDE)
    _vscode(spec)["settings"].update({f"extra.setting{index}": index for index in range(30)})
    _write(project, spec)

    message = _drift(_inspect(project, monkeypatch, expected_spec=_host(project))).message

    lines = message.splitlines()[1:]
    assert len(lines) == 21
    assert lines[-1].strip() == "... 10 more"


def test_unbuildable_expected_spec_warns_unverified(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(project, dc.APP_CLAUDE)

    def unbuildable(_image: str | None) -> dict:
        raise runtime_spec.RuntimeSpecError("host skills source vanished")

    findings = _inspect(project, monkeypatch, expected_spec=unbuildable)

    (finding,) = findings
    assert finding.severity.value == "warn"
    assert finding.check_id == "interactive.devcontainer-unverified"
    assert "host skills source vanished" in finding.message
    assert "rerun `booley doctor`" in finding.fix


def test_hand_written_auto_spec_is_not_compared(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _seed(project, dc.APP_CLAUDE)
    spec["remoteEnv"]["BOOLEY_AGENT_APP"] = "auto"
    _write(project, spec)

    def forbidden(_image: str | None) -> dict:
        raise AssertionError("a hand-written spec must not be rebuilt")

    findings = _inspect(project, monkeypatch, expected_spec=forbidden)

    assert [(f.severity.value, "hand-written" in f.message) for f in findings] == [("note", True)]


def test_inside_sandbox_compares_visible_parts(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case in ("extension", "volume", "token-seed"):
        _assert_sandbox_compares_visible_part(project, monkeypatch, case)


def _assert_sandbox_compares_visible_part(
    project: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    auth_token.store_token("SYNTHETIC_STORED_TOKEN", dc.APP_CLAUDE)
    spec = _seed(project, dc.APP_CLAUDE)
    {
        "extension": _remove_extension(vaporview.BUNDLED_VSIX),
        "volume": _remove_state_volume,
        "token-seed": _remove_token_seed,
    }[case](spec)
    _write(project, spec)

    findings = _inspect(project, monkeypatch, expected_spec=None)

    if case == "token-seed":
        # A host input: only host `booley doctor` can see it.
        assert [(f.severity.value, "Sandbox-visible" in f.message) for f in findings] == [
            ("pass", True)
        ]
        return
    message = _drift(findings).message
    if case == "extension":
        assert "customizations.vscode.extensions[]" in message
    else:
        assert "+stateVolumeTargets[]" in message
        assert "transcripts/plans are lost on every rebuild" in message


def test_inside_sandbox_fresh_spec_passes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(project, dc.APP_CODEX)

    findings = _inspect(project, monkeypatch, expected_spec=None)

    assert [f.severity.value for f in findings] == ["pass"]
    assert "host `booley doctor` checks the rest" in findings[0].message


def test_inside_sandbox_malformed_cli_record_is_unverified(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _seed(project, dc.APP_NONE)
    spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = "claude=bogus"
    _write(project, spec)

    (finding,) = _inspect(project, monkeypatch, expected_spec=None)

    assert finding.check_id == "interactive.devcontainer-unverified"


def test_spec_without_run_args_still_compares(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _seed(project, dc.APP_CLAUDE)
    del spec["runArgs"]
    _write(project, spec)

    message = _drift(_inspect(project, monkeypatch, expected_spec=_host(project))).message

    assert "+runArgs[]" in message


def test_current_interactive_spec_reports_build_failures_as_spec_errors(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(project, dc.APP_CLAUDE)

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise OSError("unreadable")

    monkeypatch.setattr(init_cmd, "_current_interactive_spec_sources", refuse)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="unreadable"):
        init_cmd.current_interactive_spec(project, _IMAGE_A)

    def refuse_spec(*_args: object, **_kwargs: object) -> object:
        raise runtime_spec.RuntimeSpecError("no trusted launcher")

    monkeypatch.setattr(init_cmd, "_current_interactive_spec_sources", refuse_spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="no trusted launcher"):
        init_cmd.current_interactive_spec(project, _IMAGE_A)


def test_spec_differences_ignores_dict_order_and_keeps_list_order() -> None:
    assert dc.spec_differences({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}) == []
    swapped = dc.spec_differences({"b": [1, 2]}, {"b": [2, 1]})
    assert swapped
    assert dc.spec_differences({"a": {}, "b": []}, {"a": {}, "b": []}) == []
    assert dc.spec_differences({"a": {"x": 1}}, {"a": {"x": 2}}) == ["-a.x: 2", "+a.x: 1"]


def test_without_issuance_labels_removes_only_issuance_pairs() -> None:
    spec = {
        "runArgs": [
            "--memory=8g",
            "--label",
            "booley.spec-digest=abc",
            "--label",
            "other=keep",
            "--label",
        ]
    }

    assert runtime_spec.without_issuance_labels(spec)["runArgs"] == [
        "--memory=8g",
        "--label",
        "other=keep",
        "--label",
    ]
    assert len(spec["runArgs"]) == 6
    with pytest.raises(runtime_spec.RuntimeSpecError, match="runArgs"):
        runtime_spec.without_issuance_labels({})
