"""BOOLEY_GATEWAY_MODEL / BOOLEY_GATEWAY_EFFORT pin Codex models and effort under the gateway (#2268)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.config import agent as agent_config
from booley.config.agent import load_agent_settings, set_agent_settings
from booley.config.gateway import GatewayConfigError
from booley.core.models import AgentResult
from booley.harness import developer_probe, doctor
from booley.runtime import agent as agent_mod
from booley.runtime.agent_config import set_backend_config
from booley.specialists.mutation_tester import MutationTesterSpecialist
from booley.specialists.reviewer import ReviewerSpecialist

_PIN = "gw-model"
# A two-CLI Project: Codex is the default, the reviewer is pinned to a Claude model.
_TOML = """\
[agent]
clis = ["codex", "claude"]
default = "codex"

[models]
reviewer = "claude-opus-5-5"
mutation_tester = "gpt-6-astra"
"""
_CODEX_DEFAULT = "gpt-6.1-sol"  # coverage_analyst's built-in model on the default CLI


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A Sandbox-like gateway process whose Project pins models in booley.toml."""
    for name in (
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_AGENT_APP",
        "BOOLEY_GATEWAY_MODEL",
        "BOOLEY_GATEWAY_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "userhome"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_GATEWAY_TOKEN")
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    monkeypatch.setattr(doctor, "_agent_app_installed", lambda _provider: True)
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text(_TOML, encoding="utf-8")
    set_agent_settings(None)
    yield tmp_path
    set_agent_settings(None)
    set_backend_config(None)


def _loaded(root):
    return load_agent_settings(root, booley_dir=root / ".booley")


def _lazy():
    set_agent_settings(None)
    return agent_config.get_agent_settings()


def _doctor_findings(root, auth="subscription", provider="codex"):
    audit = doctor.ProjectAudit(
        project_root=root,
        booley_dir=root / ".booley",
        booley_toml={"agent": {"provider": provider, "auth": auth}},
        configs_toml={},
        first_target="",
    )
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(audit, reporter)
    return reporter.findings or []


def _mutation_creator_params(root, monkeypatch):
    endpoint = MutationTesterSpecialist()
    endpoint.parse_args(["--target", "sim_demo", "--scope", "rtl", "--work-dir", str(root)])
    captured = []
    monkeypatch.setattr(
        endpoint,
        "_invoke_agent_with_resume",
        lambda p: captured.append(p) or AgentResult(output=""),
    )
    monkeypatch.setattr(endpoint, "_persist_session_id", lambda _key: None)
    endpoint._invoke_creator("fixture", resume=False, attempt=1)
    return captured[0]


def test_gateway_model_pins_codex_specialists_only(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", _PIN)

    settings = _loaded(project)

    assert {name: settings.model_for_specialist(name) for name in agent_config.SPECIALISTS} == {
        "reviewer": "claude-opus-5-5",
        "mutation_tester": _PIN,
        "coverage_analyst": _PIN,
    }
    assert settings.internal_model() == _PIN
    assert settings.model_settings() == {
        "claude-opus-5-5": ("[models] reviewer",),
        _PIN: ("BOOLEY_GATEWAY_MODEL",),
    }


def test_gateway_model_pin_reaches_codex_specialist(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", _PIN)
    _lazy()

    assert MutationTesterSpecialist()._resolve_model() == _PIN
    assert ReviewerSpecialist()._resolve_model() == "claude-opus-5-5"


def test_developer_probe_uses_gateway_model(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", _PIN)
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "low")
    seen = []

    async def fake_call_agent(params):
        seen.append(params)
        raise RuntimeError("probe stops here")

    monkeypatch.setattr(agent_mod, "call_agent", fake_call_agent)
    monkeypatch.setattr(developer_probe, "configure_agent_call", lambda params: params)

    with pytest.raises(developer_probe.ProbeError):
        developer_probe.measure_developer_rss(
            project, getrusage=lambda _who: SimpleNamespace(ru_maxrss=1)
        )

    assert (seen[0].model, seen[0].reasoning_effort) == (_PIN, "low")


def test_gateway_effort_overrides_codex_effort(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", " Low ")
    settings = _lazy()

    assert (settings.effort_for("codex"), settings.effort_for("claude")) == ("low", None)
    assert MutationTesterSpecialist()._resolve_effort() == "low"
    assert ReviewerSpecialist()._resolve_effort() is None  # runs on Claude
    for level in ("none", "persistent"):  # Codex variants beyond the old tier names
        monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", level)
        assert _lazy().effort_for("codex") == level


def test_gateway_effort_reaches_mutation_creator_call(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "low")
    _lazy()

    assert _mutation_creator_params(project, monkeypatch).reasoning_effort == "low"


def test_gateway_invalid_effort_fails_loudly(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "turbo")

    for resolve in (lambda: _loaded(project), _lazy):
        with pytest.raises(GatewayConfigError) as raised:
            resolve()
        assert "BOOLEY_GATEWAY_EFFORT" in str(raised.value)
        assert "turbo" not in str(raised.value)

    monkeypatch.delenv("BOOLEY_GATEWAY_EFFORT")
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", "gw model")
    with pytest.raises(GatewayConfigError, match="BOOLEY_GATEWAY_MODEL"):
        _loaded(project)


def test_doctor_fails_invalid_gateway_effort(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "turbo")

    findings = _doctor_findings(project)

    failed = [f for f in findings if f.severity.value == "fail"]
    assert [f.check_id for f in failed] == ["agent.credential-policy"]
    assert "BOOLEY_GATEWAY_EFFORT" in failed[0].message
    assert "turbo" not in failed[0].message


def test_gateway_model_without_effort_keeps_default_effort(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", _PIN)
    settings = _loaded(project)

    assert settings.model_for_specialist("coverage_analyst") == _PIN
    assert settings.effort_for("codex") == "high"
    assert _mutation_creator_params(project, monkeypatch).reasoning_effort is None


def test_doctor_notes_gateway_model_vars_ignored_outside_gateway(project, monkeypatch):
    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH")
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", "secret-model-id")
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "xhigh")

    findings = [f for f in _doctor_findings(project) if "BOOLEY_GATEWAY_" in f.message]

    assert [f.severity.value for f in findings] == ["note"]
    assert "BOOLEY_GATEWAY_MODEL" in findings[0].message
    assert "BOOLEY_GATEWAY_EFFORT" in findings[0].message
    assert "secret-model-id" not in findings[0].message
    assert "xhigh" not in findings[0].message

    # The gateway policy with the Claude provider ignores the pin too: note, no pin FAIL.
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "turbo")
    claude = [
        f
        for f in _doctor_findings(project, "gateway", "claude")
        if "BOOLEY_GATEWAY_E" in f.message
    ]
    assert [f.severity.value for f in claude] == ["note"]


def test_gateway_model_vars_ignored_under_other_policies(project, monkeypatch):
    """Guard: outside the gateway-in-a-container Codex case nothing changes, even for bad values."""
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", _PIN)
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "turbo")

    # (a) container, subscription auth; (b) gateway export on a host.
    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH")
    subscription = _loaded(project)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setattr(agent_config, "_inside_container", lambda: False)
    host = _loaded(project)
    for settings in (subscription, host):
        assert settings.model_for_specialist("mutation_tester") == "gpt-6-astra"
        assert settings.model_for_specialist("coverage_analyst") == _CODEX_DEFAULT
        assert settings.internal_model() == "gpt-6-luna"
        assert settings.effort_for("codex") == "high"

    # (c) gateway in a container for a Claude-only Project keeps Claude models and no effort.
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    toml = project / ".booley" / "booley.toml"
    toml.write_text('[agent]\nprovider = "claude"\n', encoding="utf-8")
    claude = _loaded(project)
    assert set(claude.model_settings()) == {"claude-opus-5-5"}
    assert claude.internal_model() == "claude-sonnet-5-5"
    assert claude.effort_for("claude") is None
