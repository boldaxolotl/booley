"""Doctor reports the Sandbox egress mode through the real reporter (#2132)."""

from __future__ import annotations

import socket

import pytest

from booley.audit.diagnostic_results import Severity
from booley.audit.doctor_waivers import load_doctor_waivers
from booley.harness import doctor
from booley.runtime import runtime_context

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@pytest.fixture(autouse=True)
def _sandbox_env(monkeypatch):
    """Pin a Sandbox venue with no ambient egress, proxy, or agent state."""
    for var in ("BOOLEY_EGRESS", "CLAUDECODE", *_PROXY_VARS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


def _run_checks(monkeypatch, reporter, resolver) -> None:
    """Drive the in-Sandbox container checks with stubbed runtime probes."""
    from booley.harness import activity_hooks, web_isolation
    from booley.runtime import dashboard_extension

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
    monkeypatch.setattr(activity_hooks, "hooks_error", lambda: None)
    monkeypatch.setattr(dashboard_extension, "installed_error", lambda: None)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    doctor._run_container_checks(
        None,
        None,
        "booley-sandbox",
        False,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
    )


def _resolves(host, *_args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]


def _unresolvable(host, *_args):
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _findings(reporter, needle: str):
    return [f for f in reporter.findings if needle in f.message]


def test_unproxied_warns_through_real_reporter(monkeypatch):
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    reporter = doctor._Reporter.create()
    _run_checks(monkeypatch, reporter, _unresolvable)
    [warning] = _findings(reporter, "Unproxied Sandbox")
    assert warning.severity is Severity.WARN
    assert warning.check_id == "sandbox.unproxied"
    assert not _findings(reporter, "does not resolve")


def test_unproxied_warning_is_waivable(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    (tmp_path / "doctor-waivers.toml").write_text(
        'version = 1\n[[waiver]]\ncheck = "sandbox.unproxied"\n'
        'reason = "Codespace has no Booley proxy."\npermanent = true\n'
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(tmp_path))
    _run_checks(monkeypatch, reporter, _unresolvable)
    [finding] = _findings(reporter, "Unproxied Sandbox")
    assert finding.severity is Severity.WAIVED


def test_unresolved_proxy_fails_with_fix(monkeypatch):
    reporter = doctor._Reporter.create()
    _run_checks(monkeypatch, reporter, _unresolvable)
    [failure] = _findings(reporter, "booley-proxy")
    assert failure.severity is Severity.FAIL
    assert "BOOLEY_EGRESS=unproxied" in failure.fix


def test_resolved_proxy_passes(monkeypatch):
    reporter = doctor._Reporter.create()
    _run_checks(monkeypatch, reporter, _resolves)
    [passed] = _findings(reporter, "booley-proxy")
    assert passed.severity is Severity.PASS


@pytest.mark.parametrize(
    ("delivered", "host"),
    [("http://proxy.corp:3128", "proxy.corp"), ("corp-gw:3128", "corp-gw")],
    ids=["with-scheme", "scheme-less"],
)
def test_delivered_proxy_host_is_the_one_resolved(monkeypatch, delivered, host):
    """A proxy the environment delivered (e.g. `booley shell`) is checked, not booley-proxy."""
    monkeypatch.setenv("HTTPS_PROXY", delivered)
    looked_up: list[str] = []

    def record(name, *_args):
        looked_up.append(name)
        return _resolves(name)

    reporter = doctor._Reporter.create()
    _run_checks(monkeypatch, reporter, record)
    assert looked_up == [host]
    [passed] = _findings(reporter, host)
    assert passed.severity is Severity.PASS


def test_invalid_mode_fails_naming_values(monkeypatch):
    monkeypatch.setenv("BOOLEY_EGRESS", "bogus")
    reporter = doctor._Reporter.create()
    _run_checks(monkeypatch, reporter, _resolves)
    [failure] = _findings(reporter, "BOOLEY_EGRESS")
    assert failure.severity is Severity.FAIL
    assert "`proxy`" in failure.message and "`unproxied`" in failure.message
