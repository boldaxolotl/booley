"""BOOLEY_GATEWAY_MODEL / BOOLEY_GATEWAY_EFFORT pin Codex models and effort under the gateway (#2268)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.config import agent as agent_config
from booley.config.agent import load_agent_settings, set_agent_settings
from booley.config.gateway import GatewayConfigError
from booley.harness import developer_probe
from booley.runtime import agent as agent_mod
from booley.runtime.agent_config import set_backend_config
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from booley.specialists.reviewer import ReviewerSpecialist
from tests.harness.agent_auth_support import credential_findings, install_agent_clis, pin_venue

_PIN = "gw-model"
# A two-CLI Project: Codex is the default, the reviewer is pinned to a Claude model.
_TOML = """\
[agent]
clis = ["codex", "claude"]
default = "codex"

[models]
reviewer = "claude-opus-5-5"
coverage_analyst = "gpt-6-astra"
"""


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
    pin_venue(monkeypatch, inside=True)
    install_agent_clis(tmp_path / "userhome")
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
    return credential_findings(root, {"agent": {"provider": provider, "auth": auth}})


def test_gateway_model_pins_codex_specialists_only(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", _PIN)

    for settings in (_loaded(project), _lazy()):
        assert {
            name: settings.model_for_specialist(name) for name in agent_config.SPECIALISTS
        } == {
            "reviewer": "claude-opus-5-5",
            "coverage_analyst": _PIN,
        }
        assert settings.internal_model() == _PIN
        assert settings.model_settings() == {
            "claude-opus-5-5": ("[models] reviewer",),
            _PIN: ("BOOLEY_GATEWAY_MODEL",),
        }
        # A model pin without an effort pin keeps the default effort.
        assert settings.effort_for("codex") == "high"


def test_developer_probe_uses_gateway_model(project, monkeypatch):
    """The probe runs the pin and effort on Codex, even when the pin is named like a Claude model."""
    pin = "claude-served-by-gateway"
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", pin)
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

    assert (seen[0].model, seen[0].reasoning_effort, seen[0].agent_cli) == (pin, "low", "codex")


def test_gateway_effort_overrides_codex_effort(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", " Low ")
    settings = _lazy()

    assert (settings.effort_for("codex"), settings.effort_for("claude")) == ("low", None)
    # private: a Specialist reads its effort only inside a full MCP tool call against a real
    # Agent CLI.
    assert CoverageAnalystSpecialist()._resolve_effort() == "low"
    assert ReviewerSpecialist()._resolve_effort() is None  # runs on Claude
    for level in ("none", "persistent"):  # Codex variants beyond the old tier names
        monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", level)
        assert _lazy().effort_for("codex") == level


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
    pin_venue(monkeypatch, inside=False)
    host = _loaded(project)
    for settings in (subscription, host):
        assert settings.model_for_specialist("coverage_analyst") == "gpt-6-astra"
        assert settings.internal_model() == "gpt-6-luna"
        assert settings.effort_for("codex") == "high"

    # (c) gateway in a container for a Claude-only Project keeps Claude models and no effort.
    pin_venue(monkeypatch, inside=True)
    toml = project / ".booley" / "booley.toml"
    toml.write_text('[agent]\nprovider = "claude"\n', encoding="utf-8")
    claude = _loaded(project)
    assert set(claude.model_settings()) == {"claude-opus-5-5"}
    assert claude.internal_model() == "claude-sonnet-5-5"
    assert claude.effort_for("claude") is None


def test_config_audits_report_nothing_for_an_invalid_gateway_variable(project, monkeypatch):
    """Doctor's config audits leave a bad BOOLEY_GATEWAY_EFFORT to the credential-policy check."""
    import tomllib

    from booley.audit.agent_schema import audit_agent_table, audit_models_table

    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "turbo")
    data = tomllib.loads(_TOML)

    for audit in (audit_agent_table(data), audit_models_table(data)):
        assert [f for f in audit.findings if f.severity.value == "fail"] == []
